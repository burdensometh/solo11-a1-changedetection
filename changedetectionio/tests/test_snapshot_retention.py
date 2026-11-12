#!/usr/bin/env python3
"""Snapshot retention, end to end: the pin button on the history page, the settings fields,
and the background pass keeping the configured window.

The retention background thread is not started under pytest (it would delete the synthetic
histories the other tests build), so these tests run the phases explicitly.
"""
import json
import os

from flask import url_for

from changedetectionio import snapshot_retention
from changedetectionio.tests.util import delete_all_watches


def _watch_with_history(client, datastore_path, contents):
    """Add a paused watch and give it one snapshot per entry in ``contents``, oldest first.

    Added paused through the datastore so the scheduler does not fire a real check and add
    snapshots of its own; the history here is exactly what was seeded.
    """
    import time

    datastore = client.application.config['DATASTORE']
    url = url_for('test_endpoint', _external=True)
    uuid = datastore.add_watch(url=url, extras={'paused': True})
    watch = datastore.data['watching'][uuid]

    base = int(time.time()) - len(contents) * 86400
    for i, text in enumerate(contents):
        watch.save_history_blob(contents=text, timestamp=base + i * 86400, snapshot_id=f"seed{i}")
    watch.commit()
    return uuid, watch, base


def test_pin_and_unpin_a_snapshot(client, live_server, measure_memory_usage, datastore_path):
    uuid, watch, base = _watch_with_history(client, datastore_path, ["one\n", "two\n", "three\n"])
    ts = str(base)  # the oldest

    assert watch.is_snapshot_pinned(ts) is False
    res = client.post(url_for('ui.ui_diff.pin_snapshot', uuid=uuid),
                      data={'timestamp': ts, 'action': 'pin'},
                      headers={'Accept': 'application/json'})
    assert res.status_code == 200, res.data
    assert res.json['pinned'] is True
    assert watch.is_snapshot_pinned(ts) is True
    assert ts in watch.get('pinned_snapshots')

    # The history page shows a star and offers "Unpin"
    page = client.get(url_for('ui.ui_diff.diff_history_page', uuid=uuid)).get_data(as_text=True)
    assert '&#9733;' in page, "a pinned version is starred in the version selectors"

    res = client.post(url_for('ui.ui_diff.pin_snapshot', uuid=uuid),
                      data={'timestamp': ts, 'action': 'unpin'},
                      headers={'Accept': 'application/json'})
    assert res.status_code == 200 and res.json['pinned'] is False
    assert watch.is_snapshot_pinned(ts) is False

    # Unknown snapshot / bad action are refused, nothing changes
    res = client.post(url_for('ui.ui_diff.pin_snapshot', uuid=uuid),
                      data={'timestamp': '9999999999', 'action': 'pin'},
                      headers={'Accept': 'application/json'})
    assert res.status_code == 404
    res = client.post(url_for('ui.ui_diff.pin_snapshot', uuid=uuid),
                      data={'timestamp': ts, 'action': 'explode'},
                      headers={'Accept': 'application/json'})
    assert res.status_code == 400

    # Without the JS the form post redirects back to the history page
    res = client.post(url_for('ui.ui_diff.pin_snapshot', uuid=uuid),
                      data={'timestamp': ts, 'action': 'pin'}, follow_redirects=False)
    assert res.status_code == 302
    assert watch.is_snapshot_pinned(ts) is True

    delete_all_watches(client)


def test_pin_requires_login_like_the_history_page(client, live_server, measure_memory_usage, datastore_path):
    """The history page itself is readable when shared_diff_access is on, but pinning writes,
    so it must still require a login."""
    uuid, watch, base = _watch_with_history(client, datastore_path, ["one\n", "two\n"])
    datastore = client.application.config['DATASTORE']
    datastore.data['settings']['application']['password'] = 'some-hashed-password'
    datastore.data['settings']['application']['shared_diff_access'] = True
    datastore.commit()

    res = client.post(url_for('ui.ui_diff.pin_snapshot', uuid=uuid),
                      data={'timestamp': str(base), 'action': 'pin'},
                      headers={'Accept': 'application/json'})
    # Anonymous -> the login manager redirects (302); the pin must not have been applied
    assert res.status_code == 302
    assert watch.is_snapshot_pinned(str(base)) is False

    datastore.data['settings']['application']['password'] = False
    datastore.commit()
    delete_all_watches(client)


def test_retention_policy_from_settings_page(client, live_server, measure_memory_usage, datastore_path):
    datastore = client.application.config['DATASTORE']

    # The defaults are 30 / 30 and enabled
    assert snapshot_retention.resolve_policy(datastore) == (True, 30, 30)

    res = client.post(url_for("settings.settings_page"),
                      data={"application-snapshot_retention_enabled": "y",
                            "application-snapshot_retention_keep_last": "5",
                            "application-snapshot_retention_keep_days": "7",
                            "application-fetch_backend": "html_requests"},
                      follow_redirects=True)
    assert res.status_code == 200
    assert snapshot_retention.resolve_policy(datastore) == (True, 5, 7)

    # Unticking the checkbox switches retention off (no key is submitted for an unticked box)
    res = client.post(url_for("settings.settings_page"),
                      data={"application-snapshot_retention_keep_last": "5",
                            "application-snapshot_retention_keep_days": "7",
                            "application-fetch_backend": "html_requests"},
                      follow_redirects=True)
    assert snapshot_retention.resolve_policy(datastore)[0] is False
    assert datastore.data['settings']['application']['snapshot_retention_enabled'] is False


def test_pass_keeps_window_and_pins(client, live_server, measure_memory_usage, datastore_path):
    """A full retention pass on a real datastore: newest 2 kept, pinned kept, the rest gone."""
    import time

    datastore = client.application.config['DATASTORE']
    datastore.data['settings']['application'].update({
        'snapshot_retention_keep_last': 2,
        'snapshot_retention_keep_days': 30,
    })
    datastore.commit()

    uuid, watch, base = _watch_with_history(
        client, datastore_path, ["one\n", "two\n", "three\n", "four\n", "five\n"])
    pinned = str(base)          # oldest snapshot, outside the 2-newest window
    watch.pin_snapshot(pinned)
    watch.commit()
    assert len(watch.history) == 5

    summary = snapshot_retention.run_pass(datastore, now=time.time())
    assert summary['marked'] >= 1
    assert summary['deleted'] >= 1

    kept = list(watch.history.keys())
    assert len(kept) == 3, f"two newest + one pinned, got {kept}"
    assert pinned in kept
    assert str(base + 3 * 86400) in kept      # second-newest
    assert str(base + 4 * 86400) in kept      # current
    for ts in kept:
        assert os.path.isfile(watch.history[ts]), "the index must not point at a deleted file"

    # No leftover markers or temp files
    assert not any(name.startswith('.history-') or name == snapshot_retention.MARKER_FILENAME
                   for name in os.listdir(watch.data_dir))

    delete_all_watches(client)


def test_pass_is_resumable_on_a_real_datastore(client, live_server, measure_memory_usage, datastore_path):
    """Interrupting between batches leaves a marker; a later pass finishes exactly the same work."""
    import time

    datastore = client.application.config['DATASTORE']
    datastore.data['settings']['application'].update({
        'snapshot_retention_keep_last': 2, 'snapshot_retention_keep_days': 0,
    })
    datastore.commit()
    uuid, watch, base = _watch_with_history(
        client, datastore_path, [f"{i}\n" for i in range(8)])

    snapshot_retention.mark_watch(watch, now=time.time())
    assert snapshot_retention.load_marker(watch) is not None

    # One small batch, then "the process stops"
    first = snapshot_retention.reclaim_watch(watch, batch_size=2)
    assert first['finalized'] is False and first['remaining'] == 4

    # The restart's pass resumes and finishes
    snapshot_retention.run_pass(datastore, now=time.time())
    assert snapshot_retention.load_marker(watch) is None
    assert len(watch.history) == 2
    assert not any(name.endswith('.tmp') for name in os.listdir(watch.data_dir))

    delete_all_watches(client)


def test_watch_json_carries_pinned_snapshots(client, live_server, measure_memory_usage, datastore_path):
    """pinned_snapshots is persisted in watch.json, and a reload keeps it."""
    uuid, watch, base = _watch_with_history(client, datastore_path, ["one\n", "two\n"])
    assert watch.pin_snapshot(str(base)) is True
    watch.commit()

    path = os.path.join(datastore_path, uuid, 'watch.json')
    with open(path) as f:
        saved = json.load(f)
    assert saved['pinned_snapshots'] == [str(base)]

    datastore = client.application.config['DATASTORE']
    datastore.reload_state(datastore_path=datastore_path, include_default_watches=False,
                           version_tag=datastore.data.get('version_tag', '0.0.0'))
    assert datastore.data['watching'][uuid].get('pinned_snapshots') == [str(base)]
    delete_all_watches(client)
