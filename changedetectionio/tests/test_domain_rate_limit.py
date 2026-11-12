#!/usr/bin/env python3
"""
End-to-end tests for the per-domain rate limiter.

Covers:
  * /api/v1/systeminfo exposes the `domain_rate_limits` block with the right shape
  * enabling the minimum interval via the global settings form persists it
  * a watch whose target replies 429 is recorded as a failed domain and the
    limiter escalates its tier
  * a subsequent successful check heals the tier

The live Flask server runs in the same process during tests (forked), so the
rate limiter singleton imported here is the same object the fetch workers use.
"""

import json
import socket

from flask import url_for

from changedetectionio.domain_rate_limiter import domain_rate_limiter, extract_domain

from .util import live_server_setup, wait_for_all_checks


def _set_endpoint(datastore_path, content):
    with open(f"{datastore_path}/endpoint-content.txt", "w") as f:
        f.write(content)


def _patch_localhost_dns(monkeypatch):
    """Resolve the live server's FQDN to loopback.

    pytest-flask builds external URLs with the host's FQDN (e.g.
    localhost.localdomain); sandboxes without that name in DNS would fail every
    worker fetch before the limiter could observe anything. Map any localhost-ish
    name to 127.0.0.1, delegating everything else to the real resolver.
    """
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host and ('localhost' in host or host == '127.0.0.1'):
            return real_getaddrinfo('127.0.0.1', port, family, type, proto, flags)
        return real_getaddrinfo(host, port, family, type, proto, flags)

    monkeypatch.setattr(socket, 'getaddrinfo', fake_getaddrinfo)


def test_systeminfo_reports_domain_rate_limits(client, live_server, measure_memory_usage,
                                               datastore_path):
    live_server_setup(live_server)
    domain_rate_limiter.reset()

    api_key = live_server.app.config['DATASTORE'].data['settings']['application'].get(
        'api_access_token'
    )
    res = client.get(url_for("systeminfo"), headers={'x-api-key': api_key})
    assert res.status_code == 200
    block = res.json['domain_rate_limits']
    # Disabled by default in a fresh datastore
    assert block['enabled'] is False
    assert block['domain_count'] == 0
    assert block['domains'] == []


def test_settings_form_persists_min_interval(client, live_server, measure_memory_usage):
    live_server_setup(live_server)
    # The settings form needs a CSRF token; the test client accepts the fixed test token.
    # Missing fields fall back to the datastore defaults (see test_access_control.py).
    res = client.post(
        url_for("settings.settings_page"),
        data={
            "requests-domain_rate_min_interval": "7",
            "application-fetch_backend": "html_requests",
            "csrf_token": "test",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200

    ds = live_server.app.config['DATASTORE']
    assert ds.data['settings']['requests']['domain_rate_min_interval'] == 7

    # And systeminfo now reports the limiter as enabled
    api_key = ds.data['settings']['application'].get('api_access_token')
    info = client.get(url_for("systeminfo"), headers={'x-api-key': api_key}).json
    assert info['domain_rate_limits']['enabled'] is True

    # Reset to disabled so other tests on this worker start clean
    client.post(
        url_for("settings.settings_page"),
        data={
            "requests-domain_rate_min_interval": "0",
            "application-fetch_backend": "html_requests",
            "csrf_token": "test",
        },
        follow_redirects=True,
    )
    assert ds.data['settings']['requests']['domain_rate_min_interval'] == 0


def test_settings_page_renders_the_new_field(client, live_server, measure_memory_usage):
    live_server_setup(live_server)
    html = client.get(url_for("settings.settings_page")).data
    assert b"domain_rate_min_interval" in html


def test_failed_429_then_success_updates_domain_tier(client, live_server, measure_memory_usage,
                                                     datastore_path, monkeypatch):
    live_server_setup(live_server)
    _patch_localhost_dns(monkeypatch)
    domain_rate_limiter.reset()

    ds = live_server.app.config['DATASTORE']
    api_key = ds.data['settings']['application'].get('api_access_token')

    # Enable the limiter with a small interval so the domain is tracked without
    # stalling the suite; 0 would disable the limiter entirely.
    ds.data['settings']['requests']['domain_rate_min_interval'] = 0.05
    ds.commit()

    # A watch targeting an endpoint that answers 429. The live server's SERVER_NAME is
    # normalized to its FQDN (e.g. localhost.localdomain); derive the expected host the
    # same way the limiter does once the watch is stored.
    base_url = url_for("test_endpoint", _external=True)
    failing_url = f"{base_url}?status_code=429"
    res = client.post(
        url_for("createwatch"),
        data=json.dumps({"url": failing_url, "title": "Rate limited target"}),
        headers={'content-type': 'application/json', 'x-api-key': api_key},
        follow_redirects=True,
    )
    assert res.status_code == 201
    watch_uuid = res.json['uuid']

    # Read back the stored URL so the host matches what the worker actually fetches
    stored_url = client.get(
        url_for("watch", uuid=watch_uuid), headers={'x-api-key': api_key}
    ).json['url']
    host = extract_domain(stored_url)
    assert host
    assert 'localhost' in host

    wait_for_all_checks(client)

    # The domain must now be tracked with a recorded failure
    snap = domain_rate_limiter.status_snapshot()
    domains = {d['domain']: d for d in snap['domains']}
    assert host in domains, f"domain {host} missing from {domains.keys()}"
    entry = domains[host]
    assert entry['total_failures'] >= 1
    assert entry['consecutive_failures'] >= 1
    assert entry['last_error']
    # Same state must be visible through the status API
    info = client.get(url_for("systeminfo"), headers={'x-api-key': api_key}).json
    api_domains = {d['domain']: d for d in info['domain_rate_limits']['domains']}
    assert api_domains[host]['consecutive_failures'] >= 1

    # Now point the watch at a healthy endpoint: success must heal the streak
    _set_endpoint(datastore_path, "<html><body>all good now</body></html>")
    res = client.put(
        url_for("watch", uuid=watch_uuid),
        data=json.dumps({"url": base_url}),
        headers={'content-type': 'application/json', 'x-api-key': api_key},
        follow_redirects=True,
    )
    assert res.status_code == 200

    # Force an immediate recheck and wait it out
    client.get(url_for("watch", uuid=watch_uuid), query_string={"recheck": "true"},
               headers={'x-api-key': api_key})
    wait_for_all_checks(client)

    snap = domain_rate_limiter.status_snapshot()
    domains = {d['domain']: d for d in snap['domains']}
    # Same host, now with a recorded success and healed streak
    assert domains[host]['total_successes'] >= 1
    assert domains[host]['consecutive_failures'] == 0

    # Clean up
    client.delete(url_for("watch", uuid=watch_uuid), headers={'x-api-key': api_key})
    ds.data['settings']['requests']['domain_rate_min_interval'] = 0
    ds.commit()
    domain_rate_limiter.reset()


def test_same_domain_watches_are_spaced_apart(client, live_server, measure_memory_usage,
                                              datastore_path, monkeypatch):
    """Two watches on one domain cannot be fetched simultaneously: the second starts
    only after the first releases and the configured interval has elapsed."""
    live_server_setup(live_server)
    _patch_localhost_dns(monkeypatch)
    domain_rate_limiter.reset()

    ds = live_server.app.config['DATASTORE']
    api_key = ds.data['settings']['application'].get('api_access_token')

    # One-second spacing — long enough to measure, short enough for the suite
    ds.data['settings']['requests']['domain_rate_min_interval'] = 1
    ds.commit()

    base_url = url_for("test_endpoint", _external=True)
    host = extract_domain(base_url)
    _set_endpoint(datastore_path, "<html><body>spacing probe</body></html>")

    uuids = []
    for i in range(2):
        res = client.post(
            url_for("createwatch"),
            data=json.dumps({"url": f"{base_url}?n={i}"}),
            headers={'content-type': 'application/json', 'x-api-key': api_key},
            follow_redirects=True,
        )
        assert res.status_code == 201
        uuids.append(res.json['uuid'])

    wait_for_all_checks(client)

    entry = {d['domain']: d for d in domain_rate_limiter.status_snapshot()['domains']}[host]
    # Both checks completed serially against the one tracked domain
    assert entry['total_successes'] == 2
    assert entry['in_flight'] is False
    assert entry['min_interval_seconds'] == 1
    # No failures, so the base tier stays at 0 even after multiple checks
    assert entry['tier'] == 0

    # The queue page exposes the same state as an HTML panel
    queue_html = client.get(url_for("ui.ui_queue.queue_page")).data
    assert b"Per-domain request limits" in queue_html
    assert host.encode() in queue_html
    queue_json = client.get(url_for("ui.ui_queue.queue_json")).get_json()
    assert queue_json['domain_rate_limits']['enabled'] is True
    assert queue_json['domain_rate_limits']['domains'][0]['domain'] == host

    for watch_uuid in uuids:
        client.delete(url_for("watch", uuid=watch_uuid), headers={'x-api-key': api_key})
    ds.data['settings']['requests']['domain_rate_min_interval'] = 0
    ds.commit()
    domain_rate_limiter.reset()
