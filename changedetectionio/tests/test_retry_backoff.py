#!/usr/bin/env python3
"""
Failure-aware retry/backoff, end to end through the real scheduler + workers.

Transient failures (timeout, HTTP 5xx) are retried after 1s, 5s, 30s, at most three times, and
the state lives in the watch so the scheduler can queue the retry without waiting for the normal
time-between-check interval. Permanent failures (401/404) are recorded and never retried.
"""

import os
import time

from flask import url_for

from changedetectionio import retry

from .util import delete_all_watches, wait_for_all_checks

# A very long normal recheck interval: any check seen by the test can only come from the retry
# mechanism, not from the ordinary schedule.
NORMAL_RECHECK = {'weeks': None, 'days': None, 'hours': 180, 'minutes': None, 'seconds': None}


def _add_watch(client, url, **extras):
    datastore = client.application.config['DATASTORE']
    extras.setdefault('time_between_check_use_default', False)
    extras.setdefault('time_between_check', dict(NORMAL_RECHECK))
    return datastore.add_watch(url=url, extras=extras)


def _seed_endpoint_content(datastore_path):
    """Give /test-endpoint a normal 200 body, so a test that expects a failure does not pass by
    accident on an unrelated missing-content error."""
    from changedetectionio.tests.util import set_original_response
    set_original_response(datastore_path=datastore_path)


def test_timeout_failure_schedules_a_backoff_retry(client, live_server, measure_memory_usage, datastore_path):
    """A timeout stores the next retry in the watch metadata, and the scheduler queues it - without
    waiting for the normal time-between-check interval."""
    datastore = client.application.config['DATASTORE']

    # The test endpoint sleeps 2s, the system timeout is 1s (set by the settings form below).
    # requests' own adapter must not retry the timeout itself, or the app-level policy is hidden.
    delay = 2
    test_url = url_for('test_endpoint', delay=delay, _external=True)
    os.environ['REQUESTS_RETRY_MAX_COUNT'] = '0'

    client.post(
        url_for("settings.settings_page"),
        data={"requests-time_between_check": '{"weeks": null, "days": null, "hours": 180, "minutes": null, "seconds": null}',
              "requests-timeout": delay - 1,
              'application-fetch_backend': "html_requests"},
        follow_redirects=True
    )

    uuid = _add_watch(client, test_url)
    client.post(url_for("ui.form_watch_checknow"), follow_redirects=True)
    wait_for_all_checks(client)

    watch = datastore.data['watching'][uuid]
    assert 'Read timed out' in str(watch.get('last_error')), watch.get('last_error')
    first = watch.get('retry')
    assert first and first['kind'] == 'timeout', f"A timeout must record a retry/backoff schedule, got {first}"
    assert first['attempts'] >= 1
    # The first two backoff steps are 1s and 5s (the retry may already have fired once while the
    # check was being waited on, the scheduler loop runs every 0.01s in pytest)
    assert first['next_retry_at'] - first['failed_at'] in (1, 5), first

    # Take over the state to test the scheduler deterministically: a retry that is not yet due must
    # not be queued (the normal interval here is 180 hours), and the one that is due must be picked
    # up straight away.
    watch['last_checked'] = time.time()
    watch['retry'] = {'attempts': 1, 'check_started_at': watch['last_checked'],
                      'failed_at': watch['last_checked'], 'kind': 'timeout',
                      'next_retry_at': watch['last_checked'] + 3600, 'status_code': None}
    watch.commit()

    from changedetectionio import worker_pool
    time.sleep(3)
    assert uuid not in worker_pool.get_running_uuids(), "A retry that is not due must not be checked"
    assert watch['retry']['attempts'] == 1

    # Make it due, with no check since the failure
    watch['retry']['next_retry_at'] = watch['last_checked'] - 1
    watch['retry']['check_started_at'] = watch['last_checked']
    watch.commit()

    deadline = time.time() + 15
    while time.time() < deadline:
        state = watch.get('retry') or {}
        if state.get('attempts') == 2:
            break
        time.sleep(0.2)
    state = watch.get('retry')
    assert state and state['attempts'] == 2, \
        f"The scheduler should have queued the due retry on its own, retry state is {state}"
    assert 5 - 1.5 <= state['next_retry_at'] - state['failed_at'] <= 5 + 1.5, \
        f"The second retry must wait 5s, got {state['next_retry_at'] - state['failed_at']}"

    wait_for_all_checks(client)
    delete_all_watches(client)
    os.environ.pop('REQUESTS_RETRY_MAX_COUNT', None)


def test_404_is_recorded_as_permanent_failure(client, live_server, measure_memory_usage, datastore_path):
    _seed_endpoint_content(datastore_path)
    datastore = client.application.config['DATASTORE']

    uuid = _add_watch(client, url_for('test_endpoint', status_code=404, _external=True))
    client.post(url_for("ui.form_watch_checknow"), follow_redirects=True)
    wait_for_all_checks(client)

    watch = datastore.data['watching'][uuid]
    retry_state = watch.get('retry')
    assert '404' in str(watch.get('last_error'))
    assert retry_state and retry_state['kind'] == 'permanent', \
        f"A 404 must be a permanent failure, got {retry_state}"
    assert not retry_state.get('next_retry_at'), "A permanent failure must never schedule a retry"

    # Far beyond any backoff delay, the scheduler still must not queue it (normal interval is 180h)
    time.sleep(3)
    from changedetectionio import worker_pool
    assert uuid not in worker_pool.get_running_uuids()
    assert not retry.is_retry_due(watch, now=time.time() + 3600), \
        "A permanent failure must never become due for a retry"

    delete_all_watches(client)


def test_401_is_recorded_as_permanent_failure(client, live_server, measure_memory_usage, datastore_path):
    _seed_endpoint_content(datastore_path)
    datastore = client.application.config['DATASTORE']

    uuid = _add_watch(client, url_for('test_endpoint', status_code=401, _external=True))
    client.post(url_for("ui.form_watch_checknow"), follow_redirects=True)
    wait_for_all_checks(client)

    retry_state = datastore.data['watching'][uuid].get('retry')
    assert retry_state and retry_state['kind'] == 'permanent', \
        f"A 401 must be a permanent failure, got {retry_state}"
    assert not retry_state.get('next_retry_at')

    delete_all_watches(client)


def test_5xx_is_retried_three_times_then_left_to_the_schedule(client, live_server, measure_memory_usage, datastore_path):
    """503 is a server failure: 1s, 5s, 30s retries, then stop."""
    _seed_endpoint_content(datastore_path)
    datastore = client.application.config['DATASTORE']

    uuid = _add_watch(client, url_for('test_endpoint', status_code=503, _external=True))
    client.post(url_for("ui.form_watch_checknow"), follow_redirects=True)
    wait_for_all_checks(client)

    watch = datastore.data['watching'][uuid]
    retry_state = watch.get('retry')
    assert '503' in str(watch.get('last_error'))
    # The 1s retry may already have fired while the first check was being waited on (the ticker
    # runs every 0.01s in pytest), so it is on attempt 1 or 2 here.
    assert retry_state and retry_state['kind'] == 'server' and retry_state['attempts'] >= 1, retry_state

    # Walk the sequence by hand from a known state (the 30s step is too slow to sit through in a
    # test): each failure must move to the next delay, and the fourth must clear the state.
    from changedetectionio.retry import plan_retry

    class Non200Error(Exception):
        def __init__(self):
            self.status_code = 503

    watch['retry'] = {'attempts': 1, 'check_started_at': retry_state['failed_at'],
                      'failed_at': retry_state['failed_at'], 'kind': 'server',
                      'next_retry_at': retry_state['failed_at'] + 1, 'status_code': 503}
    retry_state = watch['retry']

    for expected_attempt, expected_delay in ((2, 5), (3, 30)):
        now = retry_state['failed_at']
        retry_state = plan_retry(watch, Non200Error(), now=now)['retry']
        assert retry_state['attempts'] == expected_attempt, retry_state
        assert retry_state['next_retry_at'] - retry_state['failed_at'] == expected_delay, retry_state
        watch['retry'] = retry_state

    update = plan_retry(watch, Non200Error(), now=retry_state['failed_at'])
    assert update == {'retry': None}, "After three retries there must be no further retry state"

    delete_all_watches(client)


def test_successful_check_clears_the_retry_state(client, live_server, measure_memory_usage, datastore_path):
    _seed_endpoint_content(datastore_path)
    datastore = client.application.config['DATASTORE']

    # Serve a 500 first, then real content
    test_url = url_for('test_endpoint', status_code=500, _external=True)
    uuid = _add_watch(client, test_url)
    client.post(url_for("ui.form_watch_checknow"), follow_redirects=True)
    wait_for_all_checks(client)
    assert datastore.data['watching'][uuid].get('retry'), "The 500 should have scheduled a retry"

    from changedetectionio.tests.util import set_original_response
    set_original_response(datastore_path=datastore_path)  # now /test-endpoint answers 200
    datastore.update_watch(uuid=uuid, update_obj={'url': url_for('test_endpoint', _external=True)})
    client.post(url_for("ui.form_watch_checknow"), follow_redirects=True)
    wait_for_all_checks(client)

    watch = datastore.data['watching'][uuid]
    assert not watch.get('last_error')
    assert watch.get('retry') is None, "A successful check must clear the retry state"

    delete_all_watches(client)


def test_retry_state_survives_a_restart(client, live_server, measure_memory_usage, datastore_path):
    """The backoff state lives in the watch, on disk - a restart carries on with the same sequence."""
    from changedetectionio.store import ChangeDetectionStore
    datastore = client.application.config['DATASTORE']

    uuid = datastore.add_watch(url=url_for('test_endpoint', _external=True))
    watch = datastore.data['watching'][uuid]
    watch['retry'] = {'attempts': 2, 'check_started_at': 1000, 'failed_at': 1001,
                      'kind': 'timeout', 'next_retry_at': 1006, 'status_code': None}
    watch.commit()

    # Read it back the same way a restart does, straight from watch.json
    import json
    with open(os.path.join(watch.data_dir, 'watch.json')) as f:
        on_disk = json.load(f)
    assert on_disk['retry'] == watch['retry'], "The retry/backoff state must be on disk"

    # And a fresh datastore (a restart) rehydrates the same state
    datastore2 = ChangeDetectionStore(datastore_path=datastore_path, include_default_watches=False)
    datastore2.reload_state(datastore_path=datastore_path, include_default_watches=False, version_tag='test')
    reloaded = datastore2.data['watching'][uuid]
    assert reloaded['retry'] == on_disk['retry'], "The retry/backoff state must be reloaded intact"

    # And the reloaded watch continues from the saved attempts count, not from zero
    from changedetectionio.retry import plan_retry

    class WrappedTimeoutError(Exception):
        pass

    update = plan_retry(reloaded, WrappedTimeoutError('timed out'), now=1010)
    assert update['retry']['attempts'] == 3, "The next failure continues the 1/5/30 sequence from disk"
    assert update['retry']['next_retry_at'] - 1010 == 30

    delete_all_watches(client)


def test_clear_errors_clears_the_retry_state(client, live_server, measure_memory_usage, datastore_path):
    """The user clearing the error must also drop a pending retry - otherwise it fires again."""
    datastore = client.application.config['DATASTORE']

    uuid = datastore.add_watch(url=url_for('test_endpoint', _external=True))
    watch = datastore.data['watching'][uuid]
    watch['last_error'] = 'Error - 503'
    watch['retry'] = {'attempts': 2, 'check_started_at': time.time(), 'failed_at': time.time(),
                      'kind': 'server', 'next_retry_at': time.time() + 30, 'status_code': 503}
    watch.commit()

    res = client.post(url_for("ui.form_watch_list_checkbox_operations"),
                      data={'op': 'clear-errors', 'uuids': [uuid]},
                      follow_redirects=True)
    assert res.status_code == 200

    watch = datastore.data['watching'][uuid]
    assert watch.get('last_error') is False
    assert watch.get('retry') is None, "Clearing the error must drop the pending retry too"

    delete_all_watches(client)


def test_retry_does_not_hold_the_worker(client, live_server, measure_memory_usage, datastore_path):
    """The backoff is a scheduler schedule, not a sleep inside the worker - the UUID is released
    after the failed check, so other watches keep being checked meanwhile."""
    from changedetectionio import worker_pool
    datastore = client.application.config['DATASTORE']

    uuid = _add_watch(client, url_for('test_endpoint', status_code=500, _external=True))
    client.post(url_for("ui.form_watch_checknow"), follow_redirects=True)
    wait_for_all_checks(client)

    watch = datastore.data['watching'][uuid]
    assert watch.get('retry') and watch['retry']['kind'] == 'server', watch.get('retry')
    assert uuid not in worker_pool.get_running_uuids(), "No worker may be parked waiting out the backoff"

    delete_all_watches(client)
