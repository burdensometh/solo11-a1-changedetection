"""
Change-fingerprint notification records: duplicate suppression and send retries.

The same content change can be recorded by two separate checks (two history
entries with the same "before" and "after" content), and in the old flow each
check queued its own notification - the user got two identical emails.

A change is identified by the SHA-256 of the *before* content and the SHA-256
of the *after* content (plus a combined fingerprint over both). Every
notification that carries a change fingerprint gets a record in the watch's own
data directory (``notification-records.json``):

    claim at queue time   -- an existing record means this exact change was
                             already queued/sent, so the notification is skipped
    re-check before send  -- a record that is already 'sent' (or gave up as
                             'failed') means another worker handled it, skip
    send failed           -- schedule a retry, at most MAX_RETRY_ATTEMPTS times,
                             and re-check the fingerprint before each retry

Records live next to the watch (not in memory) so the dedupe survives a
restart, is shared by every notification worker thread, and is carried along in
backups. All record updates are read-modify-write under a process-wide lock.
"""
import hashlib
import json
import os
import tempfile
import threading
import time

from loguru import logger

RECORD_FILENAME = 'notification-records.json'

# How many recent change fingerprints to remember per watch. This bounds the
# file (and the lifetime of a dedupe hit) - a page reverting to an *old*
# before/after pair still notifies once 50 other changes have been recorded.
MAX_TRACKED_CHANGES = 50

# "发送失败进重试队列，最多三次" - the first send is the attempt, then up to
# three more tries.  Set to 0 to disable retrying.
MAX_RETRY_ATTEMPTS = int(os.getenv('NOTIFICATION_RETRY_ATTEMPTS', '3'))

# Retries happen through the notification runner loop; a delay of 0 retries on
# the next pass (still asynchronous, never blocking the runner).
RETRY_DELAY_SECONDS = float(os.getenv('NOTIFICATION_RETRY_DELAY_SECONDS', '0'))

STATUS_QUEUED = 'queued'
STATUS_SENDING = 'sending'
STATUS_RETRYING = 'retrying'
STATUS_SENT = 'sent'
STATUS_FAILED = 'failed'

# Process-wide: notification workers are threads, and record updates are
# read-modify-write cycles on a shared file.
_RECORDS_LOCK = threading.RLock()


def hash_content(value):
    """SHA-256 of a snapshot's content. ``None``/empty hashes to ''."""
    if value is None:
        return ''
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode('utf-8')
    else:
        raw = str(value).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def change_fingerprint(prev_snapshot, current_snapshot):
    """Return (prev_hash, current_hash, fingerprint) for a change.

    The fingerprint is derived from only the content hashes, so two checks that
    observed the same before/after content produce the same fingerprint even if
    their timestamps (and therefore the notification context) differ.
    """
    prev_hash = hash_content(prev_snapshot)
    current_hash = hash_content(current_snapshot)
    fingerprint = hashlib.sha256(f'{prev_hash}:{current_hash}'.encode('ascii')).hexdigest()
    return prev_hash, current_hash, fingerprint


def effective_change_fingerprint(watch, prev_snapshot, current_snapshot, lookback=25):
    """Fingerprint of the change these two snapshots belong to.

    A duplicate check can write a history entry whose content is identical to
    the one before it (both checks fetched the same "after" content). Its
    adjacent pair then hashes to (after, after), which would not match the
    original (before, after) notification. In that case walk back through the
    watch's history to the nearest snapshot with different content and use it
    as the "before" side, so both check records collapse to one fingerprint.
    """
    prev_hash, current_hash, fingerprint = change_fingerprint(prev_snapshot, current_snapshot)
    if prev_hash != current_hash or not watch:
        return prev_hash, current_hash, fingerprint

    try:
        timestamps = list(watch.history.keys())
    except Exception:
        return prev_hash, current_hash, fingerprint

    # timestamps[-1] is the snapshot this notification is about; skip past the
    # identical neighbour(s) and stop at the first content that actually differs.
    for timestamp in reversed(timestamps[:-1][-lookback:]):
        try:
            candidate = watch.get_history_snapshot(timestamp=timestamp)
        except Exception:
            break
        candidate_hash = hash_content(candidate)
        if candidate_hash and candidate_hash != current_hash:
            fingerprint = hashlib.sha256(f'{candidate_hash}:{current_hash}'.encode('ascii')).hexdigest()
            return candidate_hash, current_hash, fingerprint

    return prev_hash, current_hash, fingerprint


def _watch_record_path(watch):
    """Path of the watch's record file, or None when there is nowhere to store it.

    ``watch`` may be a MagicMock (unit tests, plugin code) - only a real string
    data directory is acceptable.
    """
    if not watch:
        return None
    try:
        data_dir = watch.data_dir
    except Exception:
        return None
    if not isinstance(data_dir, str) or not data_dir:
        return None
    return os.path.join(data_dir, RECORD_FILENAME)


def _read_records(path):
    if not path or not os.path.isfile(path):
        return {'version': 1, 'changes': {}}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"Notification record '{path}' is unreadable ({e}), starting a fresh one")
        return {'version': 1, 'changes': {}}
    if not isinstance(data, dict) or not isinstance(data.get('changes'), dict):
        logger.warning(f"Notification record '{path}' has an unexpected layout, starting a fresh one")
        return {'version': 1, 'changes': {}}
    data.setdefault('version', 1)
    return data


def _write_records(path, data):
    """Atomic write, same temp-file-and-rename pattern as the rest of the datastore."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile('w', delete=False, dir=directory, encoding='utf-8') as tmp:
            tmp_name = tmp.name
            json.dump(data, tmp, indent=2, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _prune(changes):
    if len(changes) <= MAX_TRACKED_CHANGES:
        return
    # Keep the most recently touched fingerprints.
    for fingerprint in sorted(changes, key=lambda fp: changes[fp].get('updated_at', 0))[:-MAX_TRACKED_CHANGES]:
        del changes[fingerprint]


def _update_record(watch, fingerprint, **fields):
    """Read-modify-write one record. Returns the record, or None if unstorable."""
    path = _watch_record_path(watch)
    if not path or not fingerprint:
        return None
    with _RECORDS_LOCK:
        data = _read_records(path)
        changes = data.setdefault('changes', {})
        record = changes.get(fingerprint) or {
            'fingerprint': fingerprint,
            'created_at': time.time(),
        }
        record.setdefault('attempts', 0)
        record.update(fields)
        record['updated_at'] = time.time()
        changes[fingerprint] = record
        _prune(changes)
        try:
            _write_records(path, data)
        except Exception as e:
            logger.error(f"Could not write notification record '{path}': {e}")
            return None
        return record


def claim_change_notification(watch, prev_hash, current_hash, fingerprint):
    """Atomically reserve this change so only one notification is queued for it.

    Returns True when the caller should queue/send the notification. Any
    existing record - queued, retrying, sent, or permanently failed - means the
    change was already handled and this copy must be skipped.
    """
    path = _watch_record_path(watch)
    if not path or not fingerprint:
        # Nothing to remember against (e.g. a watch without a data dir) - send.
        return True
    with _RECORDS_LOCK:
        data = _read_records(path)
        changes = data.setdefault('changes', {})
        existing = changes.get(fingerprint)
        if existing:
            logger.info(
                f"Notification for watch {watch.get('uuid')} skipped - change fingerprint "
                f"{fingerprint[:12]} already {existing.get('status')}"
            )
            return False
        changes[fingerprint] = {
            'fingerprint': fingerprint,
            'prev_hash': prev_hash,
            'current_hash': current_hash,
            'status': STATUS_QUEUED,
            'attempts': 0,
            'created_at': time.time(),
            'updated_at': time.time(),
        }
        _prune(changes)
        try:
            _write_records(path, data)
        except Exception as e:
            # Do not silently drop notifications because the bookkeeping failed.
            logger.error(f"Could not write notification record '{path}': {e}")
            return True
    return True


def release_change_notification(watch, fingerprint):
    """Undo a claim that never made it into the notification queue (e.g. all muted)."""
    if not fingerprint:
        return
    path = _watch_record_path(watch)
    if not path:
        return
    with _RECORDS_LOCK:
        data = _read_records(path)
        changes = data.setdefault('changes', {})
        record = changes.get(fingerprint)
        if not record or record.get('status') != STATUS_QUEUED or record.get('attempts'):
            return
        del changes[fingerprint]
        try:
            _write_records(path, data)
        except Exception as e:
            logger.error(f"Could not write notification record '{path}': {e}")


def get_change_status(watch, fingerprint):
    """Current record status for a fingerprint, or None."""
    path = _watch_record_path(watch)
    if not path or not fingerprint:
        return None
    with _RECORDS_LOCK:
        data = _read_records(path)
        record = data.get('changes', {}).get(fingerprint)
        return record.get('status') if record else None


def get_change_record(watch, fingerprint):
    """The stored record (a copy) for a fingerprint, or None."""
    path = _watch_record_path(watch)
    if not path or not fingerprint:
        return None
    with _RECORDS_LOCK:
        record = _read_records(path).get('changes', {}).get(fingerprint)
        return dict(record) if record else None


def mark_change_sending(watch, fingerprint):
    _update_record(watch, fingerprint, status=STATUS_SENDING)


def mark_change_retrying(watch, fingerprint, attempts, failed_urls=None, error=None):
    return _update_record(
        watch, fingerprint,
        status=STATUS_RETRYING,
        attempts=attempts,
        failed_urls=list(failed_urls or []),
        last_error=str(error)[:2000] if error else None,
    )


def mark_change_failed(watch, fingerprint, attempts, error=None):
    return _update_record(
        watch, fingerprint,
        status=STATUS_FAILED,
        attempts=attempts,
        last_error=str(error)[:2000] if error else None,
    )


def mark_change_sent(watch, fingerprint, sent_urls=None, attempts=None):
    fields = dict(
        status=STATUS_SENT,
        sent_at=time.time(),
        sent_urls=list(sent_urls or []),
        last_error=None,
        failed_urls=[],
    )
    if attempts is not None:
        fields['attempts'] = attempts
    return _update_record(watch, fingerprint, **fields)


def _watch_for(datastore, n_object):
    try:
        return datastore.data['watching'].get(n_object.get('uuid'))
    except Exception:
        return None


def watch_for_notification(n_object, datastore):
    """The Watch object a notification belongs to, safe for test doubles."""
    return _watch_for(datastore, n_object)


def notification_should_send(n_object, datastore):
    """The "look it up once more before sending" gate.

    Returns True only when this caller wins the right to deliver the change:
    a record already 'sent' (or permanently 'failed') means a duplicate or a
    stale retry, and two workers reaching the same record at once are serialised
    so exactly one of them proceeds.  Items without a fingerprint (filter/step
    failure alerts, tests, RSS) always pass through.
    """
    fingerprint = n_object.get('_change_fingerprint')
    if not fingerprint:
        return True
    watch = _watch_for(datastore, n_object)
    if watch is None and n_object.get('uuid'):
        # The watch was deleted while this sat in the queue/retry list.
        logger.debug(f"Notification for deleted watch {n_object.get('uuid')} dropped")
        return False
    path = _watch_record_path(watch)
    if not path:
        return True
    with _RECORDS_LOCK:
        data = _read_records(path)
        record = data.get('changes', {}).get(fingerprint)
        if record and record.get('status') in (STATUS_SENT, STATUS_FAILED):
            logger.info(
                f"Notification for watch {n_object.get('uuid')} skipped before sending - "
                f"change fingerprint {fingerprint[:12]} is already '{record.get('status')}'"
            )
            return False
        if record:
            record['status'] = STATUS_SENDING
            record['updated_at'] = time.time()
            try:
                _write_records(path, data)
            except Exception as e:
                logger.error(f"Could not write notification record '{path}': {e}")
    return True


def failed_urls_from_exception(exc):
    """URLs that actually failed, when the handler could tell us."""
    urls = getattr(exc, 'failed_urls', None)
    if urls is None:
        return None
    return [str(u) for u in urls]


def sent_urls_from_result(sent_result):
    if not isinstance(sent_result, list):
        return None
    return [o.get('url') for o in sent_result if isinstance(o, dict) and o.get('sent')]


def handle_notification_sent(n_object, datastore, sent_result=None):
    """Persist a successful delivery."""
    fingerprint = n_object.get('_change_fingerprint')
    if not fingerprint:
        return
    watch = _watch_for(datastore, n_object)
    attempts = None
    if n_object.get('_retry_attempts'):
        # _retry_attempts counts sends that already failed before this one got through
        attempts = int(n_object.get('_retry_attempts'))
    mark_change_sent(watch, fingerprint, sent_urls=sent_urls_from_result(sent_result) or n_object.get('notification_urls'),
                     attempts=attempts)


def build_retry_notification(n_object, datastore, exc):
    """Record the failure and return the notification to retry, or None when done.

    Only the URLs that failed are retried - a partial failure (one broken
    Apprise URL next to a working one) must not re-send the working one.
    """
    fingerprint = n_object.get('_change_fingerprint')
    watch = _watch_for(datastore, n_object)
    attempts = int(n_object.get('_retry_attempts', 0)) + 1
    failed_urls = failed_urls_from_exception(exc)
    if failed_urls is None:
        # Handler could not say which URL failed: assume all of them.
        failed_urls = [str(u) for u in (n_object.get('notification_urls') or [])]
    error = str(exc)

    if MAX_RETRY_ATTEMPTS <= 0 or attempts > MAX_RETRY_ATTEMPTS or not failed_urls:
        if fingerprint:
            mark_change_failed(watch, fingerprint, attempts=attempts, error=error)
        logger.error(
            f"Notification for watch {n_object.get('uuid')} gave up after {attempts} attempt(s)"
            + ("" if failed_urls else " - no failed URL left to retry")
        )
        return None

    if fingerprint:
        mark_change_retrying(watch, fingerprint, attempts=attempts, failed_urls=failed_urls, error=error)
    logger.warning(
        f"Notification for watch {n_object.get('uuid')} failed ({error.splitlines()[-1][:300]}), "
        f"retry {attempts}/{MAX_RETRY_ATTEMPTS} scheduled for {len(failed_urls)} URL(s)"
    )

    # Shallow copy: NotificationContextData is a dict subclass and the values
    # (snapshot strings, rendered diff objects) are immutable here.  A deepcopy
    # would trip over FormattableTimestamp's custom __new__.
    from changedetectionio.notification_service import NotificationContextData
    retry = NotificationContextData(dict(n_object))
    retry['notification_urls'] = failed_urls
    retry['_retry_attempts'] = attempts
    return retry


class NotificationRetryQueue:
    """In-memory queue of notifications waiting for a retry.

    Deliberately non-blocking: the notification runner checks it whenever the
    normal queue is empty, so a failing endpoint never stalls other watches.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._items = []
        self._sequence = 0

    def schedule(self, n_object, delay=None):
        delay = RETRY_DELAY_SECONDS if delay is None else delay
        with self._lock:
            self._sequence += 1
            self._items.append((time.time() + delay, self._sequence, n_object))
        return True

    def take_due(self, now=None):
        """Pop one due item (oldest first), or None."""
        now = time.time() if now is None else now
        with self._lock:
            due = [entry for entry in self._items if entry[0] <= now]
            if not due:
                return None
            entry = min(due, key=lambda e: (e[0], e[1]))
            self._items.remove(entry)
            return entry[2]

    def qsize(self):
        with self._lock:
            return len(self._items)

    def clear(self):
        with self._lock:
            self._items.clear()


# Shared by every notification runner thread in this process.
notification_retry_queue = NotificationRetryQueue()
