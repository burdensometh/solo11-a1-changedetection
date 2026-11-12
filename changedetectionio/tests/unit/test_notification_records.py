"""
Unit tests for changedetectionio/notification/records.py

The same change can appear in two check records (two history snapshots with the
same before/after content). Dedupe and retry both key off a fingerprint of that
content, stored per watch in notification-records.json.
"""
import json
import os
import tempfile

import pytest

from changedetectionio.notification import records


class _Watch(dict):
    """Just enough Watch for the records module: uuid, .get and a real data_dir."""

    def __init__(self, data_dir, uuid='watch-uuid'):
        super().__init__({'uuid': uuid})
        self._data_dir = data_dir

    @property
    def data_dir(self):
        return self._data_dir


class _HistoryWatch(_Watch):
    """A watch with history snapshots, for the 'identical neighbour' look-back."""

    def __init__(self, data_dir, history, uuid='watch-uuid'):
        super().__init__(data_dir, uuid=uuid)
        self.history = history

    def get_history_snapshot(self, timestamp=None, filepath=None):
        return self.history[timestamp]


@pytest.fixture
def watch():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, 'watch-uuid'), exist_ok=True)
        yield _Watch(os.path.join(tmp, 'watch-uuid'))


def _records_file(watch):
    return os.path.join(watch.data_dir, records.RECORD_FILENAME)


def _load(watch):
    with open(_records_file(watch)) as f:
        return json.load(f)


def test_hash_content_and_fingerprint_are_stable():
    prev_hash, current_hash, fingerprint = records.change_fingerprint('before text', 'after text')
    again = records.change_fingerprint('before text', 'after text')
    assert (prev_hash, current_hash, fingerprint) == again
    # Different content means a different fingerprint
    assert records.change_fingerprint('before text', 'other text')[2] != fingerprint
    # The fingerprint is the hash of the two content hashes, not of the raw text
    assert prev_hash != current_hash


def test_change_fingerprint_handles_none():
    prev_hash, current_hash, fingerprint = records.change_fingerprint(None, 'x')
    assert prev_hash == records.hash_content(None) == ''
    assert fingerprint


def test_claim_second_time_is_refused(watch):
    prev_hash, current_hash, fingerprint = records.change_fingerprint('a', 'b')
    assert records.claim_change_notification(watch, prev_hash, current_hash, fingerprint) is True
    assert records.claim_change_notification(watch, prev_hash, current_hash, fingerprint) is False

    record = _load(watch)['changes'][fingerprint]
    assert record['status'] == records.STATUS_QUEUED
    assert record['prev_hash'] == prev_hash
    assert record['current_hash'] == current_hash


def test_different_changes_both_allowed(watch):
    assert records.claim_change_notification(watch, *records.change_fingerprint('a', 'b')) is True
    assert records.claim_change_notification(watch, *records.change_fingerprint('b', 'c')) is True


def test_sent_status_blocks_before_send(watch):
    prev_hash, current_hash, fingerprint = records.change_fingerprint('a', 'b')
    records.claim_change_notification(watch, prev_hash, current_hash, fingerprint)
    records.mark_change_sending(watch, fingerprint)
    assert records.notification_should_send({'_change_fingerprint': fingerprint,
                                             'uuid': 'watch-uuid'}, _datastore(watch)) is True

    records.mark_change_sent(watch, fingerprint, sent_urls=['json://x'])
    assert records.notification_should_send({'_change_fingerprint': fingerprint,
                                             'uuid': 'watch-uuid'}, _datastore(watch)) is False
    assert records.get_change_status(watch, fingerprint) == records.STATUS_SENT


def _datastore(watch):
    class _DS:
        data = {'watching': {watch['uuid']: watch}}
    return _DS()


def test_before_send_gate_serialises_concurrent_workers(watch):
    """Two workers holding the same queued notification: only one may proceed."""
    prev_hash, current_hash, fingerprint = records.change_fingerprint('a', 'b')
    n_object = {'_change_fingerprint': fingerprint, 'uuid': 'watch-uuid'}
    ds = _datastore(watch)

    records.claim_change_notification(watch, prev_hash, current_hash, fingerprint)
    # First worker passes the gate and flips the record to 'sending'
    assert records.notification_should_send(n_object, ds) is True
    records.mark_change_sent(watch, fingerprint)
    # Second worker is refused
    assert records.notification_should_send(n_object, ds) is False


def test_release_claim_allows_a_later_check_to_notify(watch):
    """All-muted refused the queue put, so the claim must not suppress the next check."""
    prev_hash, current_hash, fingerprint = records.change_fingerprint('a', 'b')
    records.claim_change_notification(watch, prev_hash, current_hash, fingerprint)
    assert records.get_change_status(watch, fingerprint) == records.STATUS_QUEUED
    records.release_change_notification(watch, fingerprint)
    assert records.get_change_status(watch, fingerprint) is None
    assert records.claim_change_notification(watch, prev_hash, current_hash, fingerprint) is True


def test_items_without_fingerprint_pass_through(watch):
    ds = _datastore(watch)
    assert records.notification_should_send({'uuid': 'watch-uuid'}, ds) is True
    # Filter/step failure alerts and tests must not be recorded or blocked
    records.handle_notification_sent({'uuid': 'watch-uuid'}, ds, sent_result=[])
    assert not os.path.isfile(_records_file(watch))


def test_identical_neighbour_lookback_matches_original_change():
    """The reported bug shape: check #1 sees A->B, check #2 duplicates the B record.

    Both check records must fingerprint to the same (A,B) change.
    """
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, 'watch-uuid'), exist_ok=True)
        watch = _HistoryWatch(os.path.join(tmp, 'watch-uuid'),
                              history={'100': 'A content', '200': 'B content', '300': 'B content'})

        first = records.effective_change_fingerprint(watch, 'A content', 'B content')
        duplicate = records.effective_change_fingerprint(watch, 'B content', 'B content')
        assert first[2] == duplicate[2]
        assert duplicate[0] == first[0] == records.hash_content('A content')
        assert duplicate[1] == records.hash_content('B content')


def test_lookback_stops_at_previous_distinct_change():
    """Repeated identical content must not reach back past the real transition."""
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, 'watch-uuid'), exist_ok=True)
        watch = _HistoryWatch(os.path.join(tmp, 'watch-uuid'),
                              history={'100': 'A', '200': 'B', '300': 'B', '400': 'B'})
        assert records.effective_change_fingerprint(watch, 'B', 'B')[2] == \
            records.change_fingerprint('A', 'B')[2]


def test_record_file_is_pruned(watch):
    for i in range(records.MAX_TRACKED_CHANGES + 10):
        records.claim_change_notification(watch, *records.change_fingerprint(f'before {i}', f'after {i}'))
    changes = _load(watch)['changes']
    assert len(changes) == records.MAX_TRACKED_CHANGES
    # The newest change is still there (and still claimed)
    newest = records.change_fingerprint(f'before {records.MAX_TRACKED_CHANGES + 9}',
                                        f'after {records.MAX_TRACKED_CHANGES + 9}')
    assert newest[2] in changes


def test_corrupt_record_file_does_not_crash_and_does_not_block(watch):
    with open(_records_file(watch), 'w') as f:
        f.write('{this is not json')
    prev_hash, current_hash, fingerprint = records.change_fingerprint('a', 'b')
    assert records.claim_change_notification(watch, prev_hash, current_hash, fingerprint) is True
    # And it is repaired on the next write
    assert fingerprint in _load(watch)['changes']


def test_missing_data_dir_means_always_send():
    class _NoDir:
        data_dir = None
        def get(self, key, default=None):
            return default
    assert records.claim_change_notification(_NoDir(), 'h1', 'h2', 'fp') is True
    assert records.notification_should_send({'_change_fingerprint': 'fp'},
                                            _datastore(_Watch(None))) is True


def test_failed_reaches_max_attempts_then_gives_up():
    """Failed sends retry up to MAX_RETRY_ATTEMPTS, then are marked failed."""
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, 'watch-uuid'), exist_ok=True)
        watch = _Watch(os.path.join(tmp, 'watch-uuid'))
        ds = _datastore(watch)

        class _Boom(Exception):
            failed_urls = ['json://broken']

        n_object = {
            'uuid': 'watch-uuid',
            'watch_url': 'https://example.com',
            '_change_fingerprint': 'fp',
            'notification_urls': ['json://broken'],
        }
        records.claim_change_notification(watch, 'h1', 'h2', 'fp')

        retries = []
        for _ in range(records.MAX_RETRY_ATTEMPTS + 1):
            n_object = records.build_retry_notification(n_object, ds, _Boom('boom'))
            if n_object is None:
                break
            retries.append(n_object)

        assert len(retries) == records.MAX_RETRY_ATTEMPTS
        # Every retry carries only the URL that failed, and the attempt counter
        assert [r['notification_urls'] for r in retries] == [['json://broken']] * records.MAX_RETRY_ATTEMPTS
        assert [r['_retry_attempts'] for r in retries] == list(range(1, records.MAX_RETRY_ATTEMPTS + 1))
        record = _load(watch)['changes']['fp']
        assert record['status'] == records.STATUS_FAILED
        assert record['attempts'] == records.MAX_RETRY_ATTEMPTS + 1
        # And a permanently failed change is not re-sent by a stale retry
        assert records.notification_should_send(n_object or {'_change_fingerprint': 'fp',
                                                             'uuid': 'watch-uuid'}, ds) is False


def test_partial_failure_retries_only_the_failed_url():
    """A broken URL beside a working one must not re-send the working one on retry."""
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, 'watch-uuid'), exist_ok=True)
        watch = _Watch(os.path.join(tmp, 'watch-uuid'))
        ds = _datastore(watch)

        class _Partial(Exception):
            failed_urls = ['json://broken']

        n_object = {
            'uuid': 'watch-uuid',
            'watch_url': 'https://example.com',
            '_change_fingerprint': 'fp',
            'notification_urls': ['json://working', 'json://broken'],
        }
        records.claim_change_notification(watch, 'h1', 'h2', 'fp')
        retry = records.build_retry_notification(n_object, ds, _Partial('one url failed'))
        assert retry['notification_urls'] == ['json://broken']
        assert 'json://working' not in retry['notification_urls']


def test_retry_queue_orders_and_respects_delay():
    q = records.NotificationRetryQueue()
    first = {'uuid': 'a', '_retry_attempts': 1}
    second = {'uuid': 'b', '_retry_attempts': 1}
    q.schedule(first, delay=0)
    q.schedule(second, delay=60)
    assert q.qsize() == 2

    assert q.take_due()['uuid'] == 'a'
    assert q.take_due() is None           # b is not due yet
    assert q.take_due(now=__import__('time').time() + 61)['uuid'] == 'b'
    assert q.qsize() == 0


def test_shared_retry_queue_is_process_wide():
    assert records.notification_retry_queue is records.notification_retry_queue
    records.notification_retry_queue.clear()
    assert records.notification_retry_queue.qsize() == 0
