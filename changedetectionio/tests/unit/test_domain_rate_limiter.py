#!/usr/bin/env python3
"""
Unit tests for the per-domain serial request queue and adaptive slowdown
(changedetectionio/domain_rate_limiter.py).

These tests are pure-async and need no Flask app: a tiny fake datastore stands
in for the real settings tree.
"""

import asyncio
import time

import pytest

from changedetectionio.domain_rate_limiter import (
    DomainRateLimiter,
    DomainRateState,
    extract_domain,
)


class FakeRequestsSettings:
    def __init__(self, interval):
        self._data = {'settings': {'requests': {'domain_rate_min_interval': interval}}}

    def __getitem__(self, key):
        return self._data[key]


class FakeDatastore:
    def __init__(self, interval):
        self.data = FakeRequestsSettings(interval)


def test_extract_domain_normalizes_host():
    assert extract_domain("https://Example.COM:8443/path?q=1") == "example.com"
    assert extract_domain("http://user@host.test/") == "host.test"
    assert extract_domain("https://a.b.example.co.uk/x") == "a.b.example.co.uk"
    assert extract_domain("") is None
    assert extract_domain(None) is None
    assert extract_domain("not a url") is None  # no scheme/host
    assert extract_domain("http://[::1]:5000/") == "::1"


def test_disabled_when_interval_zero():
    limiter = DomainRateLimiter()
    ds = FakeDatastore(0)
    assert limiter.is_enabled(ds) is False

    async def scenario():
        return await limiter.acquire("https://example.com/", ds)

    assert asyncio.run(scenario()) is None
    # Nothing tracked while disabled
    assert limiter.status_snapshot(ds)['domain_count'] == 0


def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv("DOMAIN_RATE_MIN_INTERVAL", "0")
    limiter = DomainRateLimiter()
    assert limiter.is_enabled(FakeDatastore(10)) is False


def test_env_override_wins_over_datastore(monkeypatch):
    monkeypatch.setenv("DOMAIN_RATE_MIN_INTERVAL", "2")
    limiter = DomainRateLimiter()
    ds = FakeDatastore(10)
    state = asyncio.run(limiter.acquire("https://env.example/", ds))
    assert state is not None
    assert state.min_interval == 2.0
    limiter.reset()


def test_serial_spacing_for_same_domain():
    """Two sequential acquires to one domain must be at least min_interval apart."""
    limiter = DomainRateLimiter()
    ds = FakeDatastore(0.3)

    async def scenario():
        url = "https://same.example/a"
        t0 = time.monotonic()
        state1 = await limiter.acquire(url, ds)
        # First request starts immediately and holds the slot
        assert state1.in_flight is True
        limiter.record_success(state1)

        state2 = await limiter.acquire(url, ds)
        waited = time.monotonic() - t0
        assert waited >= 0.3, f"expected >=0.3s spacing, got {waited:.3f}s"
        limiter.record_success(state2)

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    snap = limiter.status_snapshot(ds)
    assert snap['enabled'] is True
    assert snap['domain_count'] == 1
    entry = snap['domains'][0]
    assert entry['domain'] == 'same.example'
    assert entry['tier'] == 0
    assert entry['total_successes'] == 2
    assert entry['consecutive_failures'] == 0


def test_different_domains_run_in_parallel():
    """Acquiring a second domain must not wait for the first domain's window."""
    limiter = DomainRateLimiter()
    ds = FakeDatastore(1.0)

    async def scenario():
        t0 = time.monotonic()
        state_a = await limiter.acquire("https://a.example/", ds)
        # Different domain: returns immediately even though A is still in flight
        state_b = await limiter.acquire("https://b.example/", ds)
        waited = time.monotonic() - t0
        assert waited < 0.1, f"different domains blocked each other for {waited:.3f}s"
        assert state_a.domain == 'a.example'
        assert state_b.domain == 'b.example'
        assert state_a.in_flight and state_b.in_flight
        limiter.record_success(state_a)
        limiter.record_success(state_b)

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert limiter.status_snapshot()['domain_count'] == 2


def test_concurrent_same_domain_requests_serialize():
    """N coroutines hitting one domain execute strictly one at a time, spaced out."""
    limiter = DomainRateLimiter()
    ds = FakeDatastore(0.15)
    url = "https://burst.example/item"
    start_times = []
    lock = asyncio.Lock()

    async def one_check():
        state = await limiter.acquire(url, ds)
        async with lock:
            start_times.append(time.monotonic())
        await asyncio.sleep(0.02)  # simulate a tiny fetch
        limiter.record_success(state)

    async def scenario():
        await asyncio.gather(*(one_check() for _ in range(4)))

    asyncio.run(asyncio.wait_for(scenario(), timeout=15))

    assert len(start_times) == 4
    start_times.sort()
    gaps = [b - a for a, b in zip(start_times, start_times[1:], strict=False)]
    assert all(g >= 0.15 - 0.02 for g in gaps), f"same-domain requests overlapped: {gaps}"
    # Total runtime: three gaps of at least 0.15s
    assert (start_times[-1] - start_times[0]) >= 0.45 - 0.02


def test_failure_tiers_escalate_and_success_heals():
    limiter = DomainRateLimiter()

    state = DomainRateState(domain='x.example', min_interval=1.0)
    # Default: 2 consecutive failures per tier step
    assert state.effective_interval == 1.0

    limiter.record_failure(state, "429")
    assert state.tier == 0
    assert state.consecutive_failures == 1
    assert state.last_error == "429"

    limiter.record_failure(state, "429 again")
    assert state.tier == 1
    assert state.effective_interval == 2.0

    limiter.record_failure(state)
    limiter.record_failure(state)
    assert state.tier == 2
    assert state.effective_interval == 4.0

    # A success drops one tier and clears the streak
    limiter.record_success(state)
    assert state.tier == 1
    assert state.consecutive_failures == 0
    assert state.last_error == ""

    # Another success heals back to base tier
    limiter.record_success(state)
    assert state.tier == 0
    assert state.effective_interval == 1.0

    # Tier never goes below zero
    limiter.record_success(state)
    assert state.tier == 0


def test_failure_tier_slowdown_is_observed_by_acquire():
    """After escalating to tier 1 the spacing really becomes 2x the base interval."""
    limiter = DomainRateLimiter()
    ds = FakeDatastore(0.2)

    async def scenario():
        url = "https://slow.example/"
        s1 = await limiter.acquire(url, ds)
        limiter.record_failure(s1, "boom")
        limiter.record_failure(s1, "boom")  # -> tier 1, effective 0.4s

        t0 = time.monotonic()
        s2 = await limiter.acquire(url, ds)
        waited = time.monotonic() - t0
        assert waited >= 0.4 - 0.05, f"tier-1 spacing not applied, waited {waited:.3f}s"
        limiter.record_success(s2)

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_in_flight_blocks_second_same_domain_request_until_release():
    """While one request is in flight, a peer coroutine for the same domain parks."""
    limiter = DomainRateLimiter()
    ds = FakeDatastore(0.05)  # small cooldown; the serial slot is what this test asserts

    async def scenario():
        s1 = await limiter.acquire("https://slot.example/", ds)
        assert s1.in_flight is True

        peer_started = asyncio.Event()

        async def peer():
            s2 = await limiter.acquire("https://slot.example/", ds)
            peer_started.set()
            limiter.record_success(s2)

        task = asyncio.ensure_future(peer())
        # Give the peer a few slices to prove it is parked, not just scheduled late
        await asyncio.sleep(0.15)
        assert not peer_started.is_set(), "same-domain request started while another was in flight"

        limiter.record_success(s1)
        await asyncio.wait_for(task, timeout=2)
        assert peer_started.is_set()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_in_flight_slot_is_released_on_failure_and_success():
    limiter = DomainRateLimiter()
    ds = FakeDatastore(0.05)

    async def scenario():
        s = await limiter.acquire("https://rel.example/", ds)
        assert s.in_flight is True
        limiter.record_failure(s, "err")
        assert s.in_flight is False
        assert s.in_flight_since is None

        s2 = await limiter.acquire("https://rel.example/", ds)
        assert s2.in_flight is True
        limiter.record_success(s2)
        assert s2.in_flight is False

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_status_snapshot_orders_throttled_domains_first():
    limiter = DomainRateLimiter()
    healthy = DomainRateState(domain='healthy.example', min_interval=1.0)
    throttled = DomainRateState(domain='throttled.example', min_interval=1.0, tier=3,
                                consecutive_failures=6)
    limiter._states['healthy.example'] = healthy
    limiter._states['throttled.example'] = throttled

    snap = limiter.status_snapshot()
    assert snap['domain_count'] == 2
    assert snap['domains'][0]['domain'] == 'throttled.example'
    assert snap['domains'][0]['tier'] == 3
    # Snapshot is a serialized copy, not the live state object
    snap['domains'][0]['tier'] = 99
    assert throttled.tier == 3


def test_reset_clears_state():
    limiter = DomainRateLimiter()
    ds = FakeDatastore(0.05)

    async def scenario():
        s = await limiter.acquire("https://gone.example/", ds)
        limiter.record_failure(s)

    asyncio.run(scenario())
    assert limiter.status_snapshot()['domain_count'] == 1
    limiter.reset('gone.example')
    assert limiter.status_snapshot()['domain_count'] == 0

    async def scenario2():
        s = await limiter.acquire("https://a.example/", ds)
        limiter.record_success(s)
        s2 = await limiter.acquire("https://b.example/", ds)
        limiter.record_success(s2)

    asyncio.run(scenario2())
    assert limiter.status_snapshot()['domain_count'] == 2
    limiter.reset()
    assert limiter.status_snapshot()['domain_count'] == 0


def test_settings_change_takes_effect_on_next_acquire():
    limiter = DomainRateLimiter()
    ds = FakeDatastore(0.3)

    async def scenario():
        s1 = await limiter.acquire("https://cfg.example/", ds)
        assert s1.min_interval == 0.3
        limiter.record_success(s1)

        # Operator raises the base interval live; the just-opened window uses
        # the old 0.3s value, the *next* release uses the new one.
        ds.data['settings']['requests']['domain_rate_min_interval'] = 0.6
        s2 = await limiter.acquire("https://cfg.example/", ds)
        assert s2.min_interval == 0.6
        # The window opened at the previous release with 0.3s, so this returns
        # after roughly the old spacing without erroring.
        limiter.record_success(s2)

        # Now the 0.6s window is in force
        t0 = time.monotonic()
        s3 = await limiter.acquire("https://cfg.example/", ds)
        waited = time.monotonic() - t0
        assert waited >= 0.6 - 0.05, f"new interval not applied, waited {waited:.3f}s"
        limiter.record_success(s3)

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_unparseable_url_is_not_limited():
    limiter = DomainRateLimiter()
    ds = FakeDatastore(5.0)

    async def scenario():
        return await limiter.acquire("", ds)

    assert asyncio.run(scenario()) is None
    assert limiter.status_snapshot()['domain_count'] == 0


def test_max_delay_caps_tier_escalation(monkeypatch):
    # A low cap keeps even a high tier bounded
    monkeypatch.setattr("changedetectionio.domain_rate_limiter.DEFAULT_MAX_DELAY", 10)
    state = DomainRateState(domain='cap.example', min_interval=2.0, tier=10)
    # 2 * 2^10 would be 2048s, capped at 10
    assert state.effective_interval == 10


def test_record_methods_accept_none():
    # The worker passes None straight through when limiting is disabled.
    limiter = DomainRateLimiter()
    limiter.record_success(None)
    limiter.record_failure(None)
    assert limiter.status_snapshot()['domain_count'] == 0


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-c', '/dev/null']))
