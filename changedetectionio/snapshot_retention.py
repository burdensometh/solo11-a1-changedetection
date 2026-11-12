"""
Snapshot retention: keep a bounded, useful history per watch and reclaim the rest.

A watch saves one snapshot per check that found a change. Over a year that is hundreds of
gigabytes for a large install, and almost all of it is old history nobody looks at. This
module applies a per-watch retention policy and deletes what falls outside it.

What is always kept, regardless of the limits below
---------------------------------------------------
  * the current version (the newest snapshot)
  * the version immediately before the most recent change (the second newest snapshot)
  * snapshots the user pinned (``watch['pinned_snapshots']``)

The limits (both apply, so a snapshot is kept only if BOTH limits allow it)
----------------------------------------------------------------------------
  keep_last    at most this many of the newest snapshots
  keep_days    nothing older than this many days

Both are configurable globally (settings -> application, defaults 30 / 30, overridable
with SNAPSHOT_RETENTION_KEEP_LAST / SNAPSHOT_RETENTION_KEEP_DAYS / SNAPSHOT_RETENTION_ENABLED).
A limit set to 0 is switched off; both at 0 means the policy keeps everything.

Timing
------
The background loop runs its first pass shortly after startup and then hourly
(SNAPSHOT_RETENTION_INITIAL_DELAY_SECONDS / SNAPSHOT_RETENTION_INTERVAL_SECONDS; set
SNAPSHOT_RETENTION_DISABLE_BACKGROUND=true to not start it at all). Each pass is
idempotent, so an extra run (a test, a manual call) never changes the outcome.
Deletion is two-phase, so an interruption never leaves the history index pointing at
files in a half-deleted state
----------------------------------------------------------------------------------------
  1. MARK     decide which timestamps expire and write ``snapshot-cleanup.json`` into the
              watch's data directory. Nothing is deleted, the history index is untouched.
  2. RECLAIM  a background task deletes the files for a batch of marked timestamps, then
              removes them from the history index and drops the marker. Progress is saved
              in the marker after every batch, so if the process stops halfway the next
              pass re-reads the marker and carries on where it left off. Deleting a file
              twice is harmless, so a marker save that was missed only costs a repeat.
              The protected rules are re-checked at this point too: a timestamp pinned
              (or promoted to current / before-the-latest-change) since it was marked is
              rescued instead of deleted.

Every mutation of a watch's history index or marker happens under the per-watch lock
below, shared with ``Watch.save_history_blob()`` / ``history_trim()``. That stops a check
appending a snapshot from racing the index rewrite.

Lock order (must always be outer -> inner, never the reverse)
------------------------------------------------------------
  datastore.lock  ->  per-watch snapshot lock

Nothing here takes the datastore lock while holding the per-watch lock: ``watch.commit()``
takes the datastore lock, so it is called OUTSIDE the per-watch locked section. The reverse
order does exist in the store (``delete()`` holds datastore.lock and removes a whole watch
directory); taking the per-watch lock there would invert the order and risk a deadlock,
which is why the reclaimer never touches the datastore lock.
"""
import glob
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager

from loguru import logger

from changedetectionio.strtobool import strtobool

MARKER_FILENAME = "snapshot-cleanup.json"
MARKER_VERSION = 1

DEFAULT_KEEP_LAST = 30
DEFAULT_KEEP_DAYS = 30

# Timestamps that are kept for ever by rule, no matter how old they are
# (current version, version before the latest change, pinned snapshots). See module docstring.

# --- per-watch lock -------------------------------------------------------------------
# Keyed by real data_dir. A plain library lock rather than the datastore lock because the
# snapshot hot path (save_history_blob) never takes the datastore lock and must not start.
_locks = {}
_locks_guard = threading.Lock()


def _watch_lock(data_dir):
    key = os.path.realpath(data_dir)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


@contextmanager
def watch_lock(data_dir):
    """Serialise snapshot index/marker changes for one watch."""
    lock = _watch_lock(data_dir)
    with lock:
        yield


# --- policy --------------------------------------------------------------------------

def _env_int(name, default):
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return default
    try:
        return int(str(value).strip())
    except ValueError:
        logger.warning(f"Invalid {name} value {value!r}, using {default}")
        return default


def defaults_from_env():
    """The application defaults, as seeded into changedetection.json (see model/App.py)."""
    enabled = os.getenv('SNAPSHOT_RETENTION_ENABLED')
    return {
        'snapshot_retention_enabled': True if enabled is None else strtobool(enabled),
        'snapshot_retention_keep_last': _env_int('SNAPSHOT_RETENTION_KEEP_LAST', DEFAULT_KEEP_LAST),
        'snapshot_retention_keep_days': _env_int('SNAPSHOT_RETENTION_KEEP_DAYS', DEFAULT_KEEP_DAYS),
    }


def resolve_policy(datastore):
    """(enabled, keep_last, keep_days) from global settings, falling back to the env defaults."""
    settings = {}
    source = datastore
    if not isinstance(source, dict):
        source = getattr(datastore, 'data', None) or {}
    try:
        settings = source['settings']['application']
    except (KeyError, TypeError):
        settings = {}
    defaults = defaults_from_env()
    enabled = settings.get('snapshot_retention_enabled')
    if enabled is None:
        enabled = defaults['snapshot_retention_enabled']
    keep_last = settings.get('snapshot_retention_keep_last')
    if keep_last is None:
        keep_last = defaults['snapshot_retention_keep_last']
    keep_days = settings.get('snapshot_retention_keep_days')
    if keep_days is None:
        keep_days = defaults['snapshot_retention_keep_days']
    try:
        keep_last = max(0, int(keep_last))
        keep_days = max(0, int(keep_days))
    except (TypeError, ValueError):
        keep_last, keep_days = DEFAULT_KEEP_LAST, DEFAULT_KEEP_DAYS
    return bool(enabled), keep_last, keep_days


# --- timestamps ----------------------------------------------------------------------

def _sorted_timestamps(watch):
    """History timestamps as ints, oldest first. Non-numeric junk keys are skipped."""
    out = []
    for key in (watch.history or {}):
        try:
            out.append(int(key))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def pinned_timestamps(watch):
    """The pinned timestamps as a set of ints, dropping entries no longer in the history."""
    raw = watch.get('pinned_snapshots') or []
    existing = set(_sorted_timestamps(watch))
    pins = set()
    for value in raw:
        try:
            ts = int(value)
        except (TypeError, ValueError):
            continue
        if ts in existing:
            pins.add(ts)
    return pins


def protected_timestamps(watch):
    """Timestamps that must never be deleted: current, before the latest change, and pinned."""
    timestamps = _sorted_timestamps(watch)
    protected = set()
    if timestamps:
        protected.add(timestamps[-1])                  # current version
        if len(timestamps) > 1:
            protected.add(timestamps[-2])              # version before the latest change
    protected |= pinned_timestamps(watch)
    return protected


def expired_timestamps(watch, now, keep_last, keep_days):
    """Timestamps outside the retention window, excluding the protected ones.

    Both limits apply: with keep_last=30 and keep_days=30 a snapshot survives only if it is
    among the newest 30 AND newer than 30 days. (Keeping the union instead would let a page
    that changes once a week keep 30 snapshots from most of a year, which is the opposite of
    the point.) A limit of 0 is switched off.
    """
    timestamps = _sorted_timestamps(watch)
    protected = protected_timestamps(watch)

    newest = set(timestamps[-keep_last:]) if keep_last > 0 else None
    cutoff = (now - keep_days * 86400) if keep_days > 0 else None

    expired = []
    for ts in timestamps:
        if ts in protected:
            continue
        if newest is not None and ts not in newest:
            expired.append(ts)
        elif cutoff is not None and ts < cutoff:
            expired.append(ts)
    return expired


# --- files belonging to a snapshot ---------------------------------------------------

_TEXT_SNAPSHOT_SUFFIXES = ('txt', 'json', 'xml', 'html', 'csv', 'md')


def snapshot_files(watch, timestamp, history=None):
    """Every file in the watch directory belonging to one history timestamp.

    Returns basenames (never paths), so a malformed marker cannot escape the data dir.
    ``history`` is an already-read {timestamp: path} map, so a batch deletion does not
    re-read the index once per timestamp.
    """
    ts = str(timestamp)
    names = set()

    if history is None:
        history = watch.history or {}
    path = history.get(ts)
    if path:
        base = os.path.basename(path)
        names.add(base)
        # A text snapshot may exist compressed or not; remove whichever variants are there.
        if base.endswith('.br'):
            names.add(base[:-3])
        else:
            root, dot, ext = base.rpartition('.')
            if dot and ext.lower() in _TEXT_SNAPSHOT_SUFFIXES:
                names.add(base + '.br')

    # Per-timestamp files written alongside the history entry
    for suffix in ('html.br', 'html', 'pdf'):
        names.add(f"{ts}.{suffix}")

    data_dir = watch.data_dir
    if data_dir and os.path.isdir(data_dir):
        # AI change-summary caches are named change-summary-{from}-to-{to}-{hash}.txt
        for pattern in (f"change-summary-{ts}-to-*.txt", f"change-summary-*-to-{ts}-*.txt"):
            for match in glob.glob(os.path.join(data_dir, pattern)):
                names.add(os.path.basename(match))

    return names


def surviving_basenames(watch, being_removed=()):
    """Basenames still referenced by a history entry that retention is NOT removing.

    Used to avoid deleting a shared snapshot file: entries are named by content hash, so
    A -> B -> A makes two timestamps point at one file, and deleting the file for the
    expired entry would silently break an entry that is being kept.
    """
    remove = {str(t) for t in being_removed}
    names = set()
    for key, path in (watch.history or {}).items():
        if str(key) not in remove:
            names.add(os.path.basename(path))
    return names


def delete_snapshot_files(watch, timestamp, keep_basenames=(), history=None):
    """Delete every file belonging to a timestamp. Missing files are fine (resume-safe).

    ``keep_basenames`` are files still referenced by a surviving history entry. Snapshot
    files are named by content hash, so the same file can back more than one timestamp
    (A -> B -> A reuses A's file); deleting it for the old entry would silently break the
    newer one. Those are skipped.
    """
    deleted = 0
    data_dir = watch.data_dir
    if not data_dir:
        return 0
    for name in snapshot_files(watch, timestamp, history=history):
        if name in keep_basenames:
            logger.debug(f"[{watch.get('uuid')}] Keeping {name}: still used by a surviving snapshot")
            continue
        path = os.path.join(data_dir, name)
        try:
            os.unlink(path)
            deleted += 1
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.warning(f"[{watch.get('uuid')}] Could not delete snapshot file {path}: {e}")
    return deleted


# --- marker --------------------------------------------------------------------------

def marker_path(watch):
    return os.path.join(watch.data_dir, MARKER_FILENAME) if watch.data_dir else None


def load_marker(watch):
    path = marker_path(watch)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            marker = json.load(f)
        if not isinstance(marker, dict) or 'timestamps' not in marker:
            raise ValueError("unrecognised marker")
        marker.setdefault('version', MARKER_VERSION)
        marker.setdefault('timestamps', [])
        marker.setdefault('done', [])
        marker['timestamps'] = [str(t) for t in marker['timestamps']]
        marker['done'] = [str(t) for t in marker['done']]
        return marker
    except (OSError, ValueError, TypeError) as e:
        logger.warning(f"[{watch.get('uuid')}] Ignoring unreadable cleanup marker {path}: {e}")
        return None


def _save_marker(watch, marker):
    path = marker_path(watch)
    if not path:
        return
    marker['updated'] = int(time.time())
    payload = json.dumps(marker, indent=2)
    fd, tmp = tempfile.mkstemp(prefix='.snapshot-cleanup-', suffix='.tmp', dir=watch.data_dir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _remove_marker(watch):
    path = marker_path(watch)
    if path and os.path.isfile(path):
        try:
            os.unlink(path)
        except OSError as e:
            logger.warning(f"[{watch.get('uuid')}] Could not remove cleanup marker {path}: {e}")


# --- phase 1: mark -------------------------------------------------------------------

def mark_watch(watch, now=None, policy=None):
    """Mark expired timestamps for later reclamation. Returns the number newly marked.

    ``policy`` is (enabled, keep_last, keep_days); resolved from the datastore when omitted.
    No files are deleted here.
    """
    if now is None:
        now = time.time()
    if policy is None:
        policy = resolve_policy(watch._datastore)
    enabled, keep_last, keep_days = policy
    if not enabled or (keep_last == 0 and keep_days == 0):
        return 0

    pins_changed = False
    newly_marked = 0
    with watch_lock(watch.data_dir):
        # Pins whose timestamps are already gone are just dead weight; tidy them as we pass.
        raw_pins = [str(p) for p in (watch.get('pinned_snapshots') or [])]
        known = {str(t) for t in _sorted_timestamps(watch)}
        cleaned_pins = [p for p in raw_pins if p in known]
        if cleaned_pins != raw_pins:
            watch['pinned_snapshots'] = cleaned_pins
            pins_changed = True

        expired = expired_timestamps(watch, now, keep_last, keep_days)
        if expired:
            marker = load_marker(watch) or {'version': MARKER_VERSION,
                                            'timestamps': [],
                                            'done': [],
                                            'created': int(now)}
            already = set(marker['timestamps'])
            newly = [t for t in expired if str(t) not in already]
            if newly:
                marker['timestamps'].extend(str(t) for t in newly)
                marker['timestamps'].sort(key=lambda x: int(x))
                _save_marker(watch, marker)
                newly_marked = len(newly)

    # Commit outside the per-watch lock: commit takes the datastore lock, and the datastore
    # lock must never be the inner lock (see the lock-order note in the module docstring).
    if pins_changed:
        try:
            watch.commit()
        except Exception as e:
            logger.warning(f"[{watch.get('uuid')}] Could not save cleaned pinned_snapshots: {e}")

    if newly_marked:
        logger.info(f"[{watch.get('uuid')}] Marked {newly_marked} history snapshot(s) for cleanup "
                    f"(keep_last={keep_last}, keep_days={keep_days}); they are deleted by the background task.")
    return newly_marked


# --- phase 2: reclaim ----------------------------------------------------------------

def _rewrite_history_index(watch, remove_timestamps):
    """Write the history index without the given timestamps, atomically."""
    index_path = os.path.join(watch.data_dir, watch.history_index_filename)
    if not os.path.isfile(index_path):
        return 0
    remove = {str(t) for t in remove_timestamps}
    removed_lines = 0
    output_lines = []
    with open(index_path, encoding='utf-8') as f:
        for line in f.readlines():
            stripped = line.strip()
            if ',' in stripped and stripped.split(',', 1)[0].strip() in remove:
                removed_lines += 1
                continue
            output_lines.append(line.rstrip('\r\n'))

    data = ("\r\n".join(output_lines) + "\r\n") if output_lines else ""
    fd, tmp = tempfile.mkstemp(prefix='.history-', suffix='.tmp', dir=watch.data_dir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, index_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return removed_lines


def reclaim_watch(watch, batch_size=None, stop_event=None, pause=0.0):
    """Delete one batch of marked files for one watch, resuming from its marker.

    Returns a dict: {'deleted': files removed, 'timestamps': timestamps finished,
                     'remaining': timestamps still to reclaim, 'finalized': bool}.

    One batch per call keeps the per-watch lock held for a bounded time; the caller
    (resume_pending / run_pass) loops until 'finalized'. If the process stops between
    batches the marker still lists the rest, so the next pass continues from there.
    """
    if batch_size is None:
        batch_size = _env_int('SNAPSHOT_RETENTION_BATCH_SIZE', 200)
    batch_size = max(1, batch_size)
    marker = load_marker(watch)
    if not marker:
        return {'deleted': 0, 'timestamps': 0, 'remaining': 0, 'finalized': False}

    summary = {'deleted': 0, 'timestamps': 0, 'remaining': 0, 'finalized': False}
    if stop_event is not None and stop_event.is_set():
        summary['remaining'] = len(set(marker['timestamps']) - set(marker['done']))
        return summary

    with watch_lock(watch.data_dir):
        marker = load_marker(watch)
        if not marker:
            return summary
        done = set(marker['done'])

        # Re-check the rules now: a snapshot pinned (or promoted to current/second-newest)
        # since it was marked must be rescued, not deleted.
        protected_now = {str(t) for t in protected_timestamps(watch)}
        rescued = [t for t in marker['timestamps'] if t in protected_now]
        if rescued:
            logger.info(f"[{watch.get('uuid')}] Rescued {len(rescued)} marked snapshot(s) that are "
                        f"now protected (pinned or became current): {rescued}")
            marker['timestamps'] = [t for t in marker['timestamps'] if t not in rescued]
            marker['done'] = [t for t in marker['done'] if t not in rescued]

        todo = [t for t in marker['timestamps'] if t not in set(marker['done'])]
        if not todo:
            # Every file is gone (or was rescued); compact the index and drop the marker.
            remove = [t for t in marker['timestamps'] if t not in protected_now]
            _rewrite_history_index(watch, remove)
            _remove_marker(watch)
            summary['finalized'] = True
            summary['remaining'] = 0
            # Force the model to re-read the index on next access
            watch.history  # noqa: B018 - property access refreshes cached counters
            logger.info(f"[{watch.get('uuid')}] Snapshot cleanup finished for {len(remove)} timestamp(s).")
            return summary

        batch = todo[:batch_size]
        removing = set(marker['timestamps'])
        keep_basenames = surviving_basenames(watch, being_removed=removing)
        # Read the index once for the whole batch: snapshot_files looks each timestamp up,
        # and a large history would otherwise be re-read once per deleted snapshot.
        history = watch.history or {}
        for ts in batch:
            # Skip if the timestamp was removed from history in the meantime; its files
            # still get an attempt, deleting is idempotent.
            summary['deleted'] += delete_snapshot_files(watch, ts, keep_basenames=keep_basenames,
                                                        history=history)
            done.add(ts)
        marker['done'] = sorted(done, key=lambda x: int(x))
        summary['timestamps'] += len(batch)
        summary['remaining'] = len(set(marker['timestamps']) - done)

        if summary['remaining']:
            _save_marker(watch, marker)
        else:
            # Last batch done - compact the index in the same locked section.
            remove = [t for t in marker['timestamps'] if t not in protected_now]
            _rewrite_history_index(watch, remove)
            _remove_marker(watch)
            summary['finalized'] = True
            watch.history  # noqa: B018
            logger.info(f"[{watch.get('uuid')}] Snapshot cleanup finished for {len(remove)} timestamp(s).")

    if pause:
        time.sleep(pause)
    return summary


# --- orchestrating a pass ------------------------------------------------------------

def _watches(datastore):
    """(uuid, watch) pairs. Accepts the real ChangeDetectionStore or a plain data dict."""
    source = datastore if isinstance(datastore, dict) else getattr(datastore, 'data', None)
    try:
        return list(source['watching'].items())
    except (KeyError, TypeError):
        return []


def resume_pending(datastore, batch_size=None, stop_event=None, pause=0.0):
    """Continue reclamation for every watch that still has a marker. Returns files deleted."""
    deleted = 0
    for _uuid, watch in _watches(datastore):
        if stop_event is not None and stop_event.is_set():
            break
        try:
            # Keep going while progress is being made, so an old backlog drains completely.
            while True:
                result = reclaim_watch(watch, batch_size=batch_size, stop_event=stop_event, pause=pause)
                deleted += result['deleted']
                if result['finalized']:
                    break
                if stop_event is not None and stop_event.is_set():
                    break
                if not result['timestamps']:
                    # Nothing progressed in this call (marker vanished, e.g. history cleared under us)
                    break
        except Exception:
            logger.opt(exception=True).error(f"[{watch.get('uuid')}] Error while reclaiming snapshots")
    return deleted


def run_pass(datastore, now=None, batch_size=None, stop_event=None, pause=0.0):
    """One retention cycle: resume marked deletions, mark new expiries, reclaim in batches.

    Order matters for the interruption guarantee: work already marked is finished before
    anything new is marked, and marking never deletes, so an interruption at any point
    leaves a marker that the next pass can pick up.
    """
    if now is None:
        now = time.time()
    summary = {'marked_watches': 0, 'marked': 0, 'deleted': 0}

    enabled, keep_last, keep_days = resolve_policy(datastore)
    if not enabled:
        # Even when switched off, finish what an earlier (enabled) pass marked.
        summary['deleted'] = resume_pending(datastore, batch_size=batch_size,
                                            stop_event=stop_event, pause=pause)
        return summary

    summary['deleted'] += resume_pending(datastore, batch_size=batch_size,
                                         stop_event=stop_event, pause=pause)

    policy = (enabled, keep_last, keep_days)
    for _uuid, watch in _watches(datastore):
        if stop_event is not None and stop_event.is_set():
            return summary
        try:
            newly = mark_watch(watch, now=now, policy=policy)
        except Exception:
            logger.opt(exception=True).error(f"[{watch.get('uuid')}] Error while marking snapshots for cleanup")
            continue
        if newly:
            summary['marked_watches'] += 1
            summary['marked'] += newly

    summary['deleted'] += resume_pending(datastore, batch_size=batch_size,
                                         stop_event=stop_event, pause=pause)
    if summary['marked']:
        logger.info(f"Snapshot retention pass: marked {summary['marked']} snapshot(s) across "
                    f"{summary['marked_watches']} watch(es), deleted {summary['deleted']} file(s).")
    return summary


def retention_loop(datastore, stop_event, interval=None, initial_delay=None):
    """Background target: run retention passes until shutdown.

    Meant to be wrapped in thread_supervisor.supervise(); any exception is logged there and
    the loop restarted, which is safe because a pass is idempotent and resumable.
    """
    if interval is None:
        interval = _env_int('SNAPSHOT_RETENTION_INTERVAL_SECONDS', 3600)
    if initial_delay is None:
        initial_delay = _env_int('SNAPSHOT_RETENTION_INITIAL_DELAY_SECONDS', 120)

    if initial_delay and stop_event.wait(initial_delay):
        return
    while not stop_event.is_set():
        run_pass(datastore, stop_event=stop_event)
        if stop_event.wait(interval):
            return
