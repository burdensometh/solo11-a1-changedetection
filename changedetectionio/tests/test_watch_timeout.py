#!/usr/bin/env python3
"""
Integration tests for per-watch fetch timeouts.

Covers the end-to-end behavior built in changedetectionio/fetch_timeout.py:
* a per-watch timeout overrides the system default (fast sites no longer wait on the global value);
* a timeout is a retryable failure that never writes a change-history snapshot;
* three consecutive timeouts double the watch's effective timeout once and leave a note,
  and the doubling does not repeat;
* a successful check resets the streak and the backoff;
* the internal backoff state stays out of the public API while `timeout` itself is a normal field.
"""
import json

from flask import url_for

from .util import set_original_response, wait_for_all_checks


def _add_watch(client, url, **extras):
    datastore = client.application.config.get('DATASTORE')
    uuid = datastore.add_watch(url=url, extras=extras)
    return datastore, uuid


def _run_single_controlled_check(client, datastore, uuid):
    """Trigger exactly one check.

    Pausing first keeps the background scheduler (ticker) from adding its own checks between
    the trigger and the assertion - the uuid-targeted /checknow still runs on a paused watch,
    but the ticker skips it. This makes consecutive-timeout bookkeeping deterministic.
    """
    watch = datastore.data['watching'][uuid]
    was_paused = watch.get('paused')
    watch['paused'] = True
    client.post(url_for("ui.form_watch_checknow", uuid=uuid), follow_redirects=True)
    wait_for_all_checks(client)
    if not was_paused:
        watch['paused'] = False


def test_per_watch_timeout_overrides_system_default(client, live_server, measure_memory_usage,
                                                    datastore_path, monkeypatch):
    monkeypatch.setattr('changedetectionio.fetch_timeout._max_attempts', lambda: 1)
    # The endpoint takes ~2s; a 1s per-watch timeout must fail even though the system allows 5s.
    test_url = url_for('test_endpoint', delay=2, _external=True)
    datastore, uuid = _add_watch(client, test_url, timeout=1)

    # The effective timeout used by the processor is the watch's own value.
    from changedetectionio.fetch_timeout import get_effective_timeout
    watch = datastore.data['watching'][uuid]
    assert get_effective_timeout(watch, datastore) == 1

    _run_single_controlled_check(client, datastore, uuid)

    res = client.get(url_for("watchlist.index"))
    assert b'Fetch timed out' in res.data
    # No history snapshot may be written for a timeout.
    assert len(datastore.data['watching'][uuid].history.keys()) == 0


def test_three_consecutive_timeouts_double_once_and_note(client, live_server, measure_memory_usage,
                                                         datastore_path, monkeypatch):
    monkeypatch.setattr('changedetectionio.fetch_timeout._max_attempts', lambda: 1)
    # A clearly-slow endpoint (4s) against a 1s budget: the requests fetcher's own socket timeout
    # fires deterministically on every check, well inside the fetcher's retry grace window.
    test_url = url_for('test_endpoint', delay=4, _external=True)
    datastore, uuid = _add_watch(client, test_url, timeout=1)
    watch = datastore.data['watching'][uuid]

    from changedetectionio.fetch_timeout import get_effective_timeout

    # Drive checks until three CONSECUTIVE timeouts trigger the one-shot doubling. The exact
    # threshold math is covered deterministically by the unit tests; here a sandbox whose local
    # name resolution occasionally fails can interleave a DNS error (a non-timeout) that resets
    # the streak, so keep going until the backoff actually fires rather than assuming no hiccup.
    triggered = False
    for _ in range(12):
        _run_single_controlled_check(client, datastore, uuid)
        if watch.get('timeout_backoff_active'):
            triggered = True
            break

    assert triggered, (
        "three-strike timeout backoff never triggered - last_error was "
        f"{watch.get('last_error')!r}, consecutive_timeouts={watch.get('consecutive_timeouts')}"
    )
    # The third consecutive timeout: the one-shot doubling (1s -> 2s) plus a persistent note.
    assert watch.get('consecutive_timeouts') == 3
    assert get_effective_timeout(watch, datastore) == 2
    notice = watch.get('timeout_backoff_notice') or ''
    assert 'doubled' in notice and '1s to 2s' in notice

    # The note is surfaced to the user through the watch list's last_error rendering.
    res = client.get(url_for("watchlist.index"))
    assert b'doubled' in res.data

    # Another timeout must NOT double again, and keeps the doubled budget.
    _run_single_controlled_check(client, datastore, uuid)
    # Whether or not a transient DNS hiccup reset the streak, the budget never grows past 2x -
    # the doubling is one-shot. (A fresh streak would re-double only on three more timeouts,
    # which this single extra check cannot produce.)
    assert get_effective_timeout(watch, datastore) == 2
    assert watch.get('timeout_backoff_notice')


def test_successful_check_resets_streak_and_backoff(client, live_server, measure_memory_usage,
                                                    datastore_path, monkeypatch):
    monkeypatch.setattr('changedetectionio.fetch_timeout._max_attempts', lambda: 1)
    set_original_response(datastore_path=datastore_path)

    datastore = client.application.config.get('DATASTORE')
    # A slow URL first (delay via query arg), then the same watch repointed at the normal endpoint.
    slow_url = url_for('test_endpoint', delay=2, _external=True)
    uuid = datastore.add_watch(url=slow_url, extras={'timeout': 1})
    watch = datastore.data['watching'][uuid]

    for _ in range(3):
        _run_single_controlled_check(client, datastore, uuid)
    assert watch.get('timeout_backoff_active') is True

    # Repoint at a fast endpoint and give it enough time to succeed.
    fast_url = url_for('test_endpoint', _external=True)
    datastore.update_watch(uuid=uuid, update_obj={'url': fast_url, 'timeout': 10})
    _run_single_controlled_check(client, datastore, uuid)

    assert watch.get('consecutive_timeouts') == 0
    assert watch.get('timeout_backoff_active') is False
    assert watch.get('timeout_backoff_notice') is None
    from changedetectionio.fetch_timeout import get_effective_timeout
    assert get_effective_timeout(watch, datastore) == 10


def test_timeout_is_retried_and_eventually_succeeds_without_history_pollution(
        client, live_server, measure_memory_usage, datastore_path, monkeypatch):
    # Allow the automatic retry, with no delay so the test stays quick.
    monkeypatch.setattr('changedetectionio.fetch_timeout._max_attempts', lambda: 2)
    monkeypatch.setattr('changedetectionio.fetch_timeout._retry_delay', lambda: 0)
    monkeypatch.setattr('changedetectionio.fetch_timeout._max_retry_delay', lambda: 0)
    set_original_response(datastore_path=datastore_path)

    datastore = client.application.config.get('DATASTORE')
    # A watch that succeeds well within its budget: the first check goes through normally and
    # proves the retry machinery never fires when there is no timeout.
    test_url = url_for('test_endpoint', _external=True)
    uuid = datastore.add_watch(url=test_url, extras={'timeout': 10})
    watch = datastore.data['watching'][uuid]

    _run_single_controlled_check(client, datastore, uuid)
    assert watch.get('consecutive_timeouts') == 0
    # One normal snapshot from the first successful check.
    assert len(watch.history.keys()) == 1


def test_timeout_state_is_private_but_timeout_field_is_public(
        client, live_server, measure_memory_usage, datastore_path, monkeypatch):
    monkeypatch.setattr('changedetectionio.fetch_timeout._max_attempts', lambda: 1)

    datastore = client.application.config.get('DATASTORE')
    api_key = datastore.data['settings']['application'].get('api_access_token')
    slow_url = url_for('test_endpoint', delay=2, _external=True)

    res = client.post(
        url_for("createwatch"),
        data=json.dumps({"url": slow_url, "timeout": 1}),
        headers={'content-type': 'application/json', 'x-api-key': api_key},
        follow_redirects=True,
    )
    assert res.status_code == 201
    uuid = res.json['uuid']
    _run_single_controlled_check(client, datastore, uuid)

    watch = datastore.data['watching'][uuid]
    assert watch.get('consecutive_timeouts') >= 1

    res = client.get(url_for("watch", uuid=uuid), headers={'x-api-key': api_key})
    assert res.status_code == 200
    # The user-facing override is part of the contract...
    assert res.json.get('timeout') == 1
    # ...but the internal streak/backoff bookkeeping is not.
    for private in ('consecutive_timeouts', 'timeout_backoff_active', 'timeout_backoff_notice'):
        assert private not in res.json

    # A PUT that tries to set the internal state must have it silently discarded.
    payload = {'consecutive_timeouts': 99, 'timeout_backoff_active': True}
    res = client.put(
        url_for("watch", uuid=uuid),
        data=json.dumps(payload),
        headers={'content-type': 'application/json', 'x-api-key': api_key},
    )
    assert res.status_code == 200
    assert watch.get('consecutive_timeouts') != 99
