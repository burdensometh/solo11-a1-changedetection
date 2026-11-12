#!/usr/bin/env python3
"""Snapshot retention: the keep rules, the mark-then-reclaim phases, and resuming.

These tests drive changedetectionio/snapshot_retention.py directly with a real Watch object
in a temporary directory, so they need no Flask server and no network.
"""
import os
import threading
import uuid as uuid_builder

import pytest

from changedetectionio import snapshot_retention as sr
from changedetectionio.model import Watch

DAY = 86400
NOW = 1_700_000_000


@pytest.fixture
def store(tmp_path):
    """A minimal datastore dict with the retention policy set explicitly."""
    return {
        'settings': {'application': {
            'snapshot_retention_enabled': True,
            'snapshot_retention_keep_last': 30,
            'snapshot_retention_keep_days': 30,
            'history_snapshot_max_length': None,
        }},
        'watching': {},
    }


def make_watch(store, tmp_path, tmp_name='watches'):
    os.makedirs(str(tmp_path / tmp_name), exist_ok=True)
    watch = Watch.model(datastore_path=str(tmp_path / tmp_name), __datastore=store,
                        default={'url': 'https://example.com', 'uuid': str(uuid_builder.uuid4())})
    watch.ensure_data_dir_exists()
    store['watching'][watch['uuid']] = watch
    return watch


def add_snapshots(watch, timestamps, contents=None):
    """Save one snapshot per timestamp. Returns {timestamp: [file paths]}."""
    files = {}
    for i, ts in enumerate(timestamps):
        text = (contents[i] if contents else f"content {ts}") + "\n"
        fname = watch.save_history_blob(contents=text, timestamp=int(ts), snapshot_id=f"snap-{ts}")
        files[int(ts)] = [os.path.join(watch.data_dir, fname)]
    return files


def reclaim_all(watch, batch_size=100):
    """Run reclamation to completion (reclaim_watch does one batch per call)."""
    while True:
        result = sr.reclaim_watch(watch, batch_size=batch_size)
        if result['finalized'] or not result['timestamps']:
            return result


def index_entries(watch):
    """Read the history index file directly.

    ``Watch.history`` drops entries whose snapshot file is gone, so mid-cleanup (files
    deleted, index not yet compacted) it already looks shorter than the index really is.
    """
    path = os.path.join(watch.data_dir, watch.history_index_filename)
    entries = []
    if os.path.isfile(path):
        with open(path) as f:
            for line in f:
                if ',' in line:
                    entries.append(line.split(',', 1)[0].strip())
    return entries


def snapshot_text(watch, ts):
    return watch.get_history_snapshot(timestamp=str(ts))


# ---------------------------------------------------------------------------------------
# keep rules
# ---------------------------------------------------------------------------------------

def test_current_and_previous_change_are_always_kept(store, tmp_path):
    """The newest snapshot and the one before the latest change survive even at keep_last=1."""
    watch = make_watch(store, tmp_path)
    add_snapshots(watch, [NOW - 10 * DAY, NOW - 5 * DAY, NOW - 1 * DAY])

    protected = sr.protected_timestamps(watch)
    assert protected == {NOW - 1 * DAY, NOW - 5 * DAY}

    expired = sr.expired_timestamps(watch, NOW, 1, 0)
    assert expired == [NOW - 10 * DAY]

    sr.mark_watch(watch, now=NOW, policy=(True, 1, 0))
    assert reclaim_all(watch, batch_size=50)['finalized'] is True
    assert list(watch.history.keys()) == [str(NOW - 5 * DAY), str(NOW - 1 * DAY)]
    assert snapshot_text(watch, NOW - 1 * DAY) == f"content {NOW - 1 * DAY}\n"


def test_keep_last_limit(store, tmp_path):
    watch = make_watch(store, tmp_path)
    timestamps = [NOW - i for i in range(10)]           # 10 snapshots, newest first in this list
    add_snapshots(watch, sorted(timestamps))

    # keep 3 newest, no age limit
    marked = sr.mark_watch(watch, now=NOW, policy=(True, 3, 0))
    assert marked == 7
    reclaim_all(watch, batch_size=100)

    kept = sorted(int(k) for k in watch.history)
    assert kept == [NOW - 2, NOW - 1, NOW]
    for ts in kept:
        assert os.path.isfile(watch.history[str(ts)])


def test_keep_days_limit_alone(store, tmp_path):
    """With keep_last switched off, only the age limit applies (plus the protected two)."""
    watch = make_watch(store, tmp_path)
    add_snapshots(watch, [NOW - 60 * DAY, NOW - 40 * DAY, NOW - 20 * DAY, NOW - 10 * DAY])

    sr.mark_watch(watch, now=NOW, policy=(True, 0, 30))
    reclaim_all(watch)

    # The 60/40 day snapshots are too old, but the 40-day one is the "before the latest
    # change" version only if it were second-newest - it isn't, so both go.
    assert sorted(int(k) for k in watch.history) == [NOW - 20 * DAY, NOW - 10 * DAY]


def test_both_limits_apply_together(store, tmp_path):
    """A snapshot must satisfy BOTH limits: within the newest N and younger than N days."""
    watch = make_watch(store, tmp_path)
    # 40 snapshots spread over 120 days, newest first: 39 recent-ish, the oldest far out
    timestamps = sorted([NOW - 3 * DAY * i for i in range(40)])
    add_snapshots(watch, timestamps)

    sr.mark_watch(watch, now=NOW, policy=(True, 30, 30))
    reclaim_all(watch, batch_size=10)

    kept = sorted(int(k) for k in watch.history)
    # 30 newest by count; of those, anything older than 30 days goes too. The newest 30 span
    # 3*29 = 87 days, so the 10 beyond 30 days go, leaving 20 + protected are inside anyway.
    assert all(NOW - ts <= 30 * DAY for ts in kept)
    assert len(kept) <= 30
    assert NOW in kept and NOW - 3 * DAY in kept


def test_limits_of_zero_mean_disabled(store, tmp_path):
    watch = make_watch(store, tmp_path)
    add_snapshots(watch, sorted(NOW - i * DAY for i in range(8)))
    assert sr.mark_watch(watch, now=NOW, policy=(True, 0, 0)) == 0
    assert sr.mark_watch(watch, now=NOW, policy=(False, 5, 5)) == 0
    assert len(watch.history) == 8


def test_pinned_snapshot_is_never_deleted_or_trimmed(store, tmp_path):
    watch = make_watch(store, tmp_path)
    timestamps = sorted(NOW - i * DAY for i in range(10))
    add_snapshots(watch, timestamps)
    old = timestamps[0]

    assert watch.pin_snapshot(old) is True
    assert watch.pin_snapshot(old) is False, "pinning twice is a no-op"
    watch.commit()

    # Retention must keep it
    sr.mark_watch(watch, now=NOW, policy=(True, 2, 0))
    reclaim_all(watch, batch_size=100)
    assert str(old) in watch.history
    assert os.path.isfile(watch.history[str(old)])

    # history_trim (the old count cap) must keep it too
    watch.history_trim(newest_n_items=2)
    assert str(old) in watch.history

    # Unpin, and the next pass is free to remove it
    assert watch.unpin_snapshot(old) is True
    sr.mark_watch(watch, now=NOW, policy=(True, 2, 0))
    reclaim_all(watch, batch_size=100)
    assert str(old) not in watch.history


def test_pin_unknown_timestamp_is_refused(store, tmp_path):
    watch = make_watch(store, tmp_path)
    add_snapshots(watch, [NOW - DAY])
    assert watch.pin_snapshot(NOW + 1000) is False
    assert watch.get('pinned_snapshots') == []


def test_stale_pins_are_dropped_when_marking(store, tmp_path):
    """A pin whose snapshot is already gone is removed so it doesn't accumulate for ever."""
    watch = make_watch(store, tmp_path)
    add_snapshots(watch, [NOW - 40 * DAY, NOW - DAY, NOW - 2 * DAY])
    watch['pinned_snapshots'] = [str(NOW - 40 * DAY), str(NOW + 12345)]  # second is unknown
    watch.commit()

    sr.mark_watch(watch, now=NOW, policy=(True, 0, 30))
    assert watch.get('pinned_snapshots') == [str(NOW - 40 * DAY)], \
        "unknown pin dropped, existing pin kept"
    reclaim_all(watch)
    assert str(NOW - 40 * DAY) in watch.history, "the pinned old snapshot survived the age limit"


def test_pinning_does_not_mark_the_watch_as_edited(store, tmp_path):
    """Pinning only changes what retention may delete; it must not force a content reprocess."""
    watch = make_watch(store, tmp_path)
    add_snapshots(watch, [NOW - DAY, NOW - 2 * DAY])
    watch.reset_watch_edited_flag()
    assert watch.was_edited is False

    watch.pin_snapshot(NOW - 2 * DAY)
    watch.unpin_snapshot(NOW - 2 * DAY)
    assert watch.was_edited is False, "pinning is not a content-relevant edit"

    # A real setting still marks it edited
    watch['title'] = 'something'
    assert watch.was_edited is True


# ---------------------------------------------------------------------------------------
# mark then reclaim
# ---------------------------------------------------------------------------------------

def test_marking_deletes_nothing(store, tmp_path):
    watch = make_watch(store, tmp_path)
    files = add_snapshots(watch, sorted(NOW - i * DAY for i in range(6)))

    marked = sr.mark_watch(watch, now=NOW, policy=(True, 2, 0))
    assert marked == 4
    for paths in files.values():
        for p in paths:
            assert os.path.isfile(p), "marking must not delete"
    assert len(watch.history) == 6
    assert sr.load_marker(watch)['timestamps'] == [str(NOW - i * DAY) for i in range(5, 1, -1)]


def test_reclaim_is_batched_and_resumable(store, tmp_path):
    """Deleting stops between batches with the marker listing the rest; a later call resumes."""
    watch = make_watch(store, tmp_path)
    timestamps = sorted(NOW - i * DAY for i in range(30))
    add_snapshots(watch, timestamps)

    sr.mark_watch(watch, now=NOW, policy=(True, 2, 0))
    marker = sr.load_marker(watch)
    assert len(marker['timestamps']) == 28

    # Simulate an interruption after one batch: only batch_size deleted, marker kept
    result = sr.reclaim_watch(watch, batch_size=5)
    assert result['finalized'] is False
    assert result['timestamps'] == 5
    assert result['remaining'] == 23
    marker = sr.load_marker(watch)
    assert len(marker['done']) == 5
    # The history index still lists everything. (Watch.history drops entries whose file is
    # gone, so read the index itself to prove the expired-but-not-yet-processed files are
    # all still there and the deleted batch is all that is gone.)
    assert len(index_entries(watch)) == 30
    for ts in marker['done']:
        assert not os.path.exists(os.path.join(watch.data_dir, f"snap-{ts}.txt")), "batch files are gone"
    for ts in set(marker['timestamps']) - set(marker['done']):
        assert os.path.isfile(os.path.join(watch.data_dir, f"snap-{ts}.txt")), "unprocessed files are still there"

    # Resume: all remaining batches run, then the index is compacted and the marker removed
    remaining = reclaim_all(watch, batch_size=5)
    assert remaining['finalized'] is True
    assert sr.load_marker(watch) is None
    assert sorted(int(k) for k in watch.history) == [NOW - DAY, NOW]


def test_resume_after_process_restart(store, tmp_path):
    """Recreate the Watch object (as a restart does) and the marker is still honoured."""
    watch = make_watch(store, tmp_path)
    add_snapshots(watch, sorted(NOW - i * DAY for i in range(8)))
    sr.mark_watch(watch, now=NOW, policy=(True, 2, 0))
    sr.reclaim_watch(watch, batch_size=2)

    # A fresh Watch object reading the same directory
    restarted = Watch.model(datastore_path=watch._datastore_path, __datastore=store,
                            default={'uuid': watch['uuid'], 'url': 'https://example.com'})
    assert sr.load_marker(restarted) is not None
    reclaim_all(restarted, batch_size=2)
    assert sorted(int(k) for k in restarted.history) == [NOW - DAY, NOW]
    assert sr.load_marker(restarted) is None


def test_marker_survives_tmp_write_failure(store, tmp_path, monkeypatch):
    """A failure writing the marker leaves the history untouched; del
    eting again is idempotent."""
    watch = make_watch(store, tmp_path)
    add_snapshots(watch, sorted(NOW - i * DAY for i in range(4)))

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(sr.os, 'replace', boom)
    with pytest.raises(OSError):
        sr.mark_watch(watch, now=NOW, policy=(True, 1, 0))
    assert len(watch.history) == 4, "nothing deleted when the marker could not be written"
    monkeypatch.undo()

    sr.mark_watch(watch, now=NOW, policy=(True, 1, 0))
    reclaim_all(watch)
    sr.mark_watch(watch, now=NOW, policy=(True, 1, 0))   # a second pass is a no-op


def test_timestamp_promoted_between_mark_and_reclaim_is_rescued(store, tmp_path):
    """The rules are checked again at reclaim time: a marked timestamp that has become
    current or second-newest since marking is rescued instead of deleted."""
    watch = make_watch(store, tmp_path)
    add_snapshots(watch, [NOW - 3 * DAY, NOW - 2 * DAY, NOW - 1 * DAY])

    sr.mark_watch(watch, now=NOW, policy=(True, 2, 0))
    # protected at marking time are NOW-1 and NOW-2, so only NOW-3 is marked
    assert sr.load_marker(watch)['timestamps'] == [str(NOW - 3 * DAY)]

    # Before the reclaimer runs, the newest snapshot disappears (e.g. an index restored
    # from an older backup, or a crash). NOW-1 becomes current and NOW-3 second-newest.
    newest = str(NOW - 1 * DAY)
    os.unlink(watch.history[newest])
    sr._rewrite_history_index(watch, [int(newest)])
    assert list(watch.history.keys()) == [str(NOW - 3 * DAY), str(NOW - 2 * DAY)]

    result = reclaim_all(watch)
    assert result['finalized'] is True
    assert sorted(int(k) for k in watch.history) == [NOW - 3 * DAY, NOW - 2 * DAY]
    # and the marker is gone, nothing was deleted
    assert sr.load_marker(watch) is None


def test_rescued_by_pin_between_mark_and_reclaim(store, tmp_path):
    watch = make_watch(store, tmp_path)
    add_snapshots(watch, sorted(NOW - i * DAY for i in range(5)))
    sr.mark_watch(watch, now=NOW, policy=(True, 2, 0))
    target = NOW - 4 * DAY
    assert str(target) in sr.load_marker(watch)['timestamps']

    watch.pin_snapshot(target)  # user pins it while it is marked
    result = reclaim_all(watch)
    assert result['finalized'] is True
    assert str(target) in watch.history
    assert os.path.isfile(watch.history[str(target)])
    assert sr.load_marker(watch) is None


def test_shared_snapshot_file_is_not_deleted(store, tmp_path):
    """A -> B -> A: two timestamps point at the same content-hash-named file. Removing the
    expired entry must not break the current one."""
    watch = make_watch(store, tmp_path)
    same = "unchanged\n"
    watch.save_history_blob(same, NOW - 3 * DAY, 'sameid')
    watch.save_history_blob("changed\n", NOW - 2 * DAY, 'otherid')
    watch.save_history_blob(same, NOW - 1 * DAY, 'sameid')
    old_path = watch.history[str(NOW - 3 * DAY)]
    new_path = watch.history[str(NOW - 1 * DAY)]
    assert old_path == new_path

    sr.mark_watch(watch, now=NOW, policy=(True, 2, 0))
    reclaim_all(watch)
    assert str(NOW - 3 * DAY) not in watch.history
    assert os.path.isfile(new_path), "the file the current snapshot uses is still there"
    assert snapshot_text(watch, NOW - 1 * DAY) == same


def test_aux_files_of_removed_snapshot_go_too(store, tmp_path):
    """The per-timestamp HTML/PDF copies and AI summary caches are removed with the snapshot."""
    watch = make_watch(store, tmp_path)
    timestamps = sorted(NOW - i * DAY for i in range(4))
    add_snapshots(watch, timestamps)
    doomed = timestamps[0]

    html_path = os.path.join(watch.data_dir, f"{doomed}.html.br")
    summary_path = os.path.join(watch.data_dir, f"change-summary-{doomed}-to-{timestamps[1]}-abcd1234.txt")
    # Write directly: save_last_fetched_html() prunes anything outside the newest two itself,
    # which would not prove anything about retention.
    with open(html_path, 'wb') as f:
        f.write(b"<html>old</html>")
    with open(summary_path, 'w') as f:
        f.write("summary")
    assert os.path.isfile(html_path) and os.path.isfile(summary_path)

    sr.mark_watch(watch, now=NOW, policy=(True, 2, 0))
    reclaim_all(watch)
    assert not os.path.isfile(html_path)
    assert not os.path.isfile(summary_path)


def test_clear_watch_removes_marker(store, tmp_path):
    """Clearing history must not leave a marker pointing at snapshots that no longer exist."""
    watch = make_watch(store, tmp_path)
    add_snapshots(watch, sorted(NOW - i * DAY for i in range(6)))
    sr.mark_watch(watch, now=NOW, policy=(True, 1, 0))
    assert sr.load_marker(watch) is not None

    watch.clear_watch()
    assert sr.load_marker(watch) is None
    assert watch.get('pinned_snapshots') == []
    assert watch.history_n == 0
    assert sr.reclaim_watch(watch)['finalized'] is False


def test_clear_watch_keeps_processor_config(store, tmp_path):
    """clear_watch wipes history data but keeps the processor config file, as before."""
    watch = make_watch(store, tmp_path)
    add_snapshots(watch, [NOW - DAY])
    config_path = os.path.join(watch.data_dir, 'restock_diff.json')
    with open(config_path, 'w') as f:
        f.write('{}')
    watch.clear_watch()
    assert os.path.isfile(config_path)


# ---------------------------------------------------------------------------------------
# pass / loop
# ---------------------------------------------------------------------------------------

def test_run_pass_marks_and_reclaims(store, tmp_path):
    watch_a = make_watch(store, tmp_path, 'a')
    watch_b = make_watch(store, tmp_path, 'b')
    # 40 daily snapshots: keep_last=30 keeps days 0..29, keep_days=30 also allows day 30,
    # so days 30..39 (10) expire. Watch B has one far older than 30 days.
    add_snapshots(watch_a, sorted(NOW - i * DAY for i in range(40)))
    add_snapshots(watch_b, [NOW - 100 * DAY, NOW - 1 * DAY, NOW - 2 * DAY])

    summary = sr.run_pass(store, now=NOW)
    assert summary['marked'] == 10 + 1
    assert summary['marked_watches'] == 2
    assert summary['deleted'] > 0

    assert len(watch_a.history) == 30
    assert sorted(int(k) for k in watch_a.history)[-1] == NOW
    assert sorted(int(k) for k in watch_b.history) == [NOW - 2 * DAY, NOW - 1 * DAY]
    assert not any(os.path.exists(os.path.join(w.data_dir, sr.MARKER_FILENAME))
                   for w in (watch_a, watch_b))


def test_run_pass_finishes_marked_work_even_when_disabled(store, tmp_path):
    """Turning retention off stops new marking but still finishes what was already marked."""
    watch = make_watch(store, tmp_path)
    add_snapshots(watch, sorted(NOW - i * DAY for i in range(6)))
    sr.mark_watch(watch, now=NOW, policy=(True, 2, 0))
    assert sr.load_marker(watch) is not None

    store['settings']['application']['snapshot_retention_enabled'] = False
    sr.run_pass(store, now=NOW)
    assert sr.load_marker(watch) is None
    assert len(watch.history) == 2

    # And with it off, nothing new is marked
    add_snapshots(watch, [NOW + 10 * DAY])
    assert sr.mark_watch(watch, now=NOW + 10 * DAY) == 0


def test_run_pass_stops_on_event(store, tmp_path):
    watch = make_watch(store, tmp_path)
    add_snapshots(watch, sorted(NOW - i * DAY for i in range(6)))
    stop = threading.Event()
    stop.set()
    summary = sr.run_pass(store, now=NOW, stop_event=stop, batch_size=2)
    assert summary['marked'] == 0, "a stopping pass does not start new marking"


def test_retention_loop_runs_then_stops(store, tmp_path, monkeypatch):
    watch = make_watch(store, tmp_path)
    add_snapshots(watch, sorted(NOW - i * DAY for i in range(6)))
    calls = []

    def fake_run_pass(ds, now=None, batch_size=None, stop_event=None, pause=0.0):
        calls.append(1)
        sr.mark_watch(watch, now=NOW, policy=(True, 2, 0))
        sr.reclaim_watch(watch)
        # Stop the loop after the first successful pass
        stop_event.set()
        return {'marked': 1}

    monkeypatch.setattr(sr, 'run_pass', fake_run_pass)
    stop = threading.Event()
    loop = threading.Thread(target=sr.retention_loop,
                            kwargs={'datastore': store, 'stop_event': stop, 'interval': 0.01,
                                    'initial_delay': 0})
    loop.start()
    loop.join(timeout=5)
    assert not loop.is_alive()
    assert len(calls) == 1
    assert len(watch.history) == 2


def test_corrupt_marker_is_ignored_and_not_fatal(store, tmp_path):
    watch = make_watch(store, tmp_path)
    add_snapshots(watch, [NOW - 2 * DAY, NOW - DAY])
    path = sr.marker_path(watch)
    with open(path, 'w') as f:
        f.write('{"not": "what i expect"')
    assert sr.load_marker(watch) is None
    assert sr.reclaim_watch(watch)['finalized'] is False
    # A later mark overwrites it cleanly
    sr.mark_watch(watch, now=NOW, policy=(True, 1, 0))


def test_policy_resolves_from_settings_and_env(store, tmp_path, monkeypatch):
    monkeypatch.setenv('SNAPSHOT_RETENTION_KEEP_LAST', '7')
    monkeypatch.setenv('SNAPSHOT_RETENTION_KEEP_DAYS', '9')
    monkeypatch.setenv('SNAPSHOT_RETENTION_ENABLED', 'false')
    defaults = sr.defaults_from_env()
    assert defaults == {'snapshot_retention_enabled': False,
                        'snapshot_retention_keep_last': 7,
                        'snapshot_retention_keep_days': 9}

    store['settings']['application'].update({'snapshot_retention_enabled': None,
                                             'snapshot_retention_keep_last': None,
                                             'snapshot_retention_keep_days': None})
    assert sr.resolve_policy(store) == (False, 7, 9)

    store['settings']['application'].update({'snapshot_retention_enabled': True,
                                             'snapshot_retention_keep_last': 3,
                                             'snapshot_retention_keep_days': 4})
    assert sr.resolve_policy(store) == (True, 3, 4)


def test_missing_policy_fields_use_defaults(store, tmp_path):
    for key in ('snapshot_retention_enabled', 'snapshot_retention_keep_last', 'snapshot_retention_keep_days'):
        store['settings']['application'].pop(key, None)
    assert sr.resolve_policy(store) == (True, 30, 30)


def test_env_seeds_a_fresh_install_but_never_overrides_saved_settings(tmp_path, monkeypatch):
    """SNAPSHOT_RETENTION_* only seeds the defaults a brand-new datastore stores; after that the
    saved settings file wins, so a startup with different env values doesn't silently undo a
    change the user made in the UI."""
    from changedetectionio.store import ChangeDetectionStore

    monkeypatch.setenv('SNAPSHOT_RETENTION_KEEP_LAST', '7')
    monkeypatch.setenv('SNAPSHOT_RETENTION_KEEP_DAYS', '9')
    monkeypatch.setenv('SNAPSHOT_RETENTION_ENABLED', 'true')
    ds = ChangeDetectionStore(datastore_path=str(tmp_path), include_default_watches=False,
                              version_tag='1.0.0')
    assert sr.resolve_policy(ds) == (True, 7, 9)

    # The user changes it in the UI (which writes changedetection.json)
    ds.data['settings']['application'].update({'snapshot_retention_enabled': False,
                                               'snapshot_retention_keep_last': 2,
                                               'snapshot_retention_keep_days': 0})
    ds.commit()

    # A later start with the original env still reads the saved values
    ds2 = ChangeDetectionStore(datastore_path=str(tmp_path), include_default_watches=False,
                               version_tag='1.0.0')
    assert sr.resolve_policy(ds2) == (False, 2, 0)


def test_invalid_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv('SNAPSHOT_RETENTION_KEEP_LAST', 'not-a-number')
    monkeypatch.setenv('SNAPSHOT_RETENTION_KEEP_DAYS', '')
    defaults = sr.defaults_from_env()
    assert defaults['snapshot_retention_keep_last'] == sr.DEFAULT_KEEP_LAST
    assert defaults['snapshot_retention_keep_days'] == sr.DEFAULT_KEEP_DAYS
