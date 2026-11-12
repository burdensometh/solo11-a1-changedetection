"""
End-to-end tests for change-fingerprint notification dedupe and the retry queue.

Both scenarios run through the real notification runner thread and a real
Apprise ``json://`` URL, so they exercise what a user would actually receive.

Note: Apprise only forwards a query parameter to the remote endpoint when it is
written ``?-name=value`` (a plain ``?name=value`` is consumed as an Apprise
option). That is why the test URLs below use ``?-``.
"""
import json
import os
import time

from flask import url_for

from .util import (
    set_original_response, set_modified_response, wait_for_all_checks, delete_all_watches,
    count_notification_hits, wait_for_notification_hits,
)


def _add_watch_with_notification(client, datastore_path, notification_url, title='dedupe'):
    set_original_response(datastore_path=datastore_path)
    test_url = url_for('test_endpoint', _external=True)
    res = client.post(
        url_for("ui.ui_views.form_quick_watch_add"),
        data={"url": test_url, "tags": ''},
        follow_redirects=True
    )
    assert b"Watch added" in res.data
    wait_for_all_checks(client)

    res = client.post(
        url_for("ui.ui_edit.edit_page", uuid="first"),
        data={"notification_urls": notification_url,
              "notification_title": f"{title} - {{{{watch_url}}}}",
              "notification_body": "Change seen: {{diff}}",
              "notification_format": 'text',
              "url": test_url,
              "tags": "",
              "title": "",
              "headers": "",
              "time_between_check-minutes": "180",
              "fetch_backend": "html_requests",
              "time_between_check_use_default": "y"},
        follow_redirects=True
    )
    assert b"Updated watch." in res.data
    wait_for_all_checks(client)
    return test_url


def _the_watch(live_server):
    """The single watch this test added (its UUID is generated, not literally 'first')."""
    watches = live_server.app.config['DATASTORE'].data['watching']
    assert len(watches) == 1, f"expected exactly one watch, got {len(watches)}"
    return watches[next(iter(watches))]


def _build_context(datastore, watch):
    from changedetectionio.notification_service import NotificationContextData, _check_cascading_vars
    n_object = NotificationContextData()
    for key in ('notification_urls', 'notification_title', 'notification_body', 'notification_format'):
        n_object[key] = _check_cascading_vars(datastore, key, watch)
    return n_object


def _record_file(watch):
    from changedetectionio.notification.records import RECORD_FILENAME
    return os.path.join(watch.data_dir, RECORD_FILENAME)


def _only_tracked_fingerprint(watch):
    with open(_record_file(watch)) as f:
        changes = json.load(f)['changes']
    assert len(changes) == 1, f"expected one tracked change, got {list(changes)}"
    return next(iter(changes))


def test_duplicate_change_record_does_not_double_notify(client, live_server, measure_memory_usage, datastore_path):
    """The reported bug: one change, two check records, two identical emails.

    Check #1 sees A -> B. A second check also records B (a duplicate history
    entry whose "before" is B as well). Both check records describe the same
    change, so exactly one notification must be delivered.
    """
    notification_url = url_for('test_notification_endpoint', _external=True).replace('http', 'json')
    _add_watch_with_notification(client, datastore_path, notification_url)

    # First change A -> B, notifies, and the change is recorded
    set_modified_response(datastore_path=datastore_path)
    client.post(url_for("ui.form_watch_checknow"), follow_redirects=True)
    wait_for_all_checks(client)
    assert wait_for_notification_hits(datastore_path, 1) == 1

    datastore = live_server.app.config['DATASTORE']
    watch = _the_watch(live_server)
    dates = list(watch.history.keys())
    assert len(dates) == 2
    fingerprint = _only_tracked_fingerprint(watch)

    # A second check records the same "after" content again: history A, B, B
    watch.save_history_blob(contents=watch.get_history_snapshot(timestamp=dates[-1]),
                            timestamp=int(dates[-1]) + 10,
                            snapshot_id='duplicate-of-last')

    from changedetectionio.flask_app import notification_q
    from changedetectionio.notification_service import create_notification_service
    from changedetectionio.notification.records import (
        effective_change_fingerprint, get_change_record,
    )

    dates = list(watch.history.keys())
    duplicate = effective_change_fingerprint(
        watch,
        watch.get_history_snapshot(timestamp=dates[-2]),
        watch.get_history_snapshot(timestamp=dates[-1]),
    )
    assert duplicate[2] == fingerprint, \
        "a duplicate check record must resolve to the same change fingerprint"

    service = create_notification_service(datastore, notification_q)
    queued = service.queue_notification_for_watch(
        n_object=_build_context(datastore, watch), watch=watch,
        date_index_from=-2, date_index_to=-1,
    )
    assert queued is None, "the duplicate change record must not queue a second notification"
    assert get_change_record(watch, fingerprint)['status'] == 'sent'

    # Still exactly one delivery reached the endpoint
    time.sleep(2)
    assert count_notification_hits(datastore_path) == 1, \
        "one change must produce exactly one notification, not two identical emails"

    delete_all_watches(client)


def test_failed_send_retries_then_succeeds(client, live_server, measure_memory_usage, datastore_path):
    """A failing endpoint is retried and delivered once it recovers."""
    # Endpoint answers 500 to the first two attempts, then 200.
    # ``?-`` makes Apprise forward the parameter to the endpoint as a GET arg.
    notification_url = url_for('test_notification_endpoint', _external=True).replace('http', 'json') + "?-fail_first=2"
    _add_watch_with_notification(client, datastore_path, notification_url, title='retry')

    set_modified_response(datastore_path=datastore_path)
    client.post(url_for("ui.form_watch_checknow"), follow_redirects=True)
    wait_for_all_checks(client)

    # Two 500s plus the successful delivery = three hits at the endpoint
    deadline = time.time() + 30
    while time.time() < deadline:
        if count_notification_hits(datastore_path) >= 3:
            break
        time.sleep(0.5)

    hits = count_notification_hits(datastore_path)
    assert hits == 3, f"expected 2 failed attempts + 1 successful delivery, got {hits} hits"

    watch = _the_watch(live_server)
    from changedetectionio.notification.records import get_change_status, get_change_record
    fingerprint = _only_tracked_fingerprint(watch)
    assert get_change_status(watch, fingerprint) == 'sent'
    record = get_change_record(watch, fingerprint)
    assert record['attempts'] == 2, "two failed sends before the delivery that got through"
    assert record['status'] == 'sent'
    assert record['failed_urls'] == [], "the failed-URL list is cleared once delivery succeeds"

    # The watch is not left showing a stale notification error
    res = client.get(url_for("watchlist.index"))
    assert b'notification-error' not in res.data

    delete_all_watches(client)


def test_failed_send_gives_up_after_three_retries(client, live_server, measure_memory_usage, datastore_path):
    """An endpoint that never recovers is tried once plus three retries, then given up."""
    notification_url = url_for('test_notification_endpoint', _external=True).replace('http', 'json') + "?-status_code=500"
    _add_watch_with_notification(client, datastore_path, notification_url, title='giveup')

    set_modified_response(datastore_path=datastore_path)
    client.post(url_for("ui.form_watch_checknow"), follow_redirects=True)
    wait_for_all_checks(client)

    from changedetectionio.notification.records import get_change_status, get_change_record, MAX_RETRY_ATTEMPTS
    watch = _the_watch(live_server)
    fingerprint = _only_tracked_fingerprint(watch)

    deadline = time.time() + 60
    while time.time() < deadline:
        if get_change_status(watch, fingerprint) == 'failed':
            break
        time.sleep(0.5)

    assert get_change_status(watch, fingerprint) == 'failed', \
        "a notification whose URL never recovers must end up 'failed' so a stale retry cannot re-send it"
    record = get_change_record(watch, fingerprint)
    assert record['attempts'] == MAX_RETRY_ATTEMPTS + 1, \
        f"expected the first send plus {MAX_RETRY_ATTEMPTS} retries"
    # First send + three retries = four attempts at the endpoint
    assert count_notification_hits(datastore_path) == MAX_RETRY_ATTEMPTS + 1

    delete_all_watches(client)
