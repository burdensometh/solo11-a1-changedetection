"""
Per-domain serial request queue with adaptive slowdown.

When many watches point at the same hostname, the fetch workers would otherwise
fire all of those requests at the same instant. Sites answer with HTTP 429 and
eventually block the domain. This module serializes requests to the same domain:

  * watches on *different* domains still run fully in parallel,
  * two requests to the *same* domain are separated by at least the configured
    minimum interval (`domain_rate_min_interval` seconds), measured from the end
    of one request to the start of the next,
  * while one request to a domain is in flight, no other request to that domain
    starts,
  * consecutive failures for a domain push it through escalating delay tiers
    (base, 2x, 4x, ...), and a successful check drops it back a tier,
  * the live state per domain (current tier, effective delay, streak) is exposed
    for the status API / queue UI.

Everything is process-global and thread-safe: workers run in separate threads,
each with its own asyncio loop, so the synchronization primitives are plain
threading ones (no loop-bound asyncio locks).
"""

import asyncio
import os
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from loguru import logger

# A single failure only counts once the streak crosses this threshold,
# so one transient blip does not immediately slow a healthy domain.
DEFAULT_FAILURES_PER_TIER = int(os.getenv("DOMAIN_RATE_FAILURES_PER_TIER", "2"))
# Hard ceiling for the effective delay, regardless of tier, in seconds.
DEFAULT_MAX_DELAY = int(os.getenv("DOMAIN_RATE_MAX_DELAY", "3600"))
# Max number of domains whose state is returned by the status snapshot,
# keeps the systeminfo payload bounded on installs with thousands of domains.
STATUS_SNAPSHOT_LIMIT = int(os.getenv("DOMAIN_RATE_STATUS_LIMIT", "500"))
# How long one sleep slice lasts while parked. Small enough that a settings
# change or shutdown is picked up promptly, cheap enough to poll.
_POLL_SLICE = 0.25


def extract_domain(url):
    """Return the hostname (lowercased, no port/userinfo) for a watch URL.

    Returns None when the URL has no usable host so callers can skip rate
    limiting instead of keying everything under an empty-string domain.
    """
    if not url:
        return None
    try:
        hostname = urlsplit(str(url)).hostname
    except (ValueError, TypeError):
        return None
    if not hostname:
        return None
    return hostname.lower()


@dataclass
class DomainRateState:
    """Mutable throttle state for one hostname."""
    domain: str
    min_interval: float
    # Monotonic time until which no new request may start (set when a request ends).
    window_ends_at: float = 0.0
    # Tier 0 = base interval, tier 1 = 2x, tier 2 = 4x, ...
    tier: int = 0
    # Consecutive failed checks since the last success.
    consecutive_failures: int = 0
    # Total failures/successes since the limiter first saw the domain.
    total_failures: int = 0
    total_successes: int = 0
    # Set while a worker holds the domain slot; only informative for the UI.
    in_flight: bool = False
    # Epoch seconds when the current in-flight request started (None when idle).
    in_flight_since: float = field(default=None)
    last_error: str = ""

    @property
    def effective_interval(self):
        delay = self.min_interval * (2 ** self.tier)
        return min(delay, DEFAULT_MAX_DELAY)

    def to_snapshot(self):
        """Plain dict for the status API."""
        return {
            'domain': self.domain,
            'tier': self.tier,
            'min_interval_seconds': round(self.min_interval, 3),
            'effective_interval_seconds': round(self.effective_interval, 3),
            'consecutive_failures': self.consecutive_failures,
            'total_failures': self.total_failures,
            'total_successes': self.total_successes,
            'in_flight': self.in_flight,
            'in_flight_since': self.in_flight_since,
            'window_ends_at': self.window_ends_at or None,
            'last_error': self.last_error or None,
        }


class DomainRateLimiter:
    """Process-wide registry of per-domain serial slots.

    Configuration is read live from the datastore settings on every acquire,
    so changing the setting in the UI takes effect without a restart or any
    rebind step.
    """

    def __init__(self):
        self._lock = threading.RLock()
        # domain -> DomainRateState
        self._states = {}

    # ------------------------------------------------------------------ config
    @staticmethod
    def _configured_min_interval(datastore):
        """Minimum seconds between the end of one request and the next start.

        0 / missing means the limiter is disabled. Read from the requests
        settings block, with an environment override for deployments that
        configure via env vars.
        """
        env_value = os.getenv("DOMAIN_RATE_MIN_INTERVAL")
        if env_value is not None:
            try:
                return max(0.0, float(env_value))
            except (TypeError, ValueError):
                logger.warning(f"DOMAIN_RATE_MIN_INTERVAL={env_value!r} is not a number, ignoring")
        try:
            value = datastore.data['settings']['requests'].get('domain_rate_min_interval', 0)
            return max(0.0, float(value or 0))
        except (AttributeError, KeyError, TypeError, ValueError):
            return 0.0

    @staticmethod
    def _failures_per_tier():
        return max(1, DEFAULT_FAILURES_PER_TIER)

    # ----------------------------------------------------------------- helpers
    def _get_or_create_state(self, domain, min_interval):
        state = self._states.get(domain)
        if state is None:
            state = DomainRateState(domain=domain, min_interval=min_interval)
            self._states[domain] = state
        else:
            # Keep the configured base interval current; tier multipliers are
            # applied on top of whatever the current setting is.
            state.min_interval = min_interval
        return state

    @staticmethod
    def _wait_seconds(state, now):
        """How much longer until this domain may be hit again.

        An in-flight request blocks the domain completely (returns inf-ish).
        Otherwise the remaining cooldown window applies.
        """
        if state.in_flight:
            return None
        return max(0.0, state.window_ends_at - now)

    # ------------------------------------------------------------------ public
    def is_enabled(self, datastore):
        return self._configured_min_interval(datastore) > 0

    async def acquire(self, url, datastore):
        """Coroutine: park until this worker may start a request for `url`.

        Returns the DomainRateState (for record_success/record_failure) or None
        when rate limiting is disabled / the URL has no parseable host.
        Yields control while waiting, so other domains keep being processed by
        the rest of the worker pool in parallel.
        """
        min_interval = self._configured_min_interval(datastore)
        if min_interval <= 0:
            return None

        domain = extract_domain(url)
        if not domain:
            return None

        with self._lock:
            state = self._get_or_create_state(domain, min_interval)

        while True:
            with self._lock:
                now = time.monotonic()
                wait = self._wait_seconds(state, now)
                if wait is None:
                    # Another request to the same domain is still running:
                    # poll until it releases the serial slot.
                    wait = _POLL_SLICE
                elif wait <= 0:
                    # Slot free and cooldown elapsed: claim it.
                    state.in_flight = True
                    state.in_flight_since = time.time()
                    return state

            # Sleep in slices instead of one long wait: settings changes and
            # shutdowns are picked up within _POLL_SLICE seconds, and the loop
            # always re-checks the real timestamps before claiming the slot.
            await asyncio.sleep(min(wait, _POLL_SLICE))

    def record_success(self, state):
        """A check completed without a fetch error: heal the failure streak."""
        if state is None:
            return
        with self._lock:
            now = time.monotonic()
            state.total_successes += 1
            if state.consecutive_failures:
                logger.info(
                    f"Domain rate limiter: {state.domain} recovered after "
                    f"{state.consecutive_failures} failure(s), healing slowdown tier"
                )
            state.consecutive_failures = 0
            # A success steps the domain back down one tier, never below base.
            if state.tier > 0:
                state.tier -= 1
            state.last_error = ""
            self._release(state, now)

    def record_failure(self, state, error=""):
        """A fetch failed: count it and escalate the delay tier in steps."""
        if state is None:
            return
        with self._lock:
            now = time.monotonic()
            state.total_failures += 1
            state.consecutive_failures += 1
            if error:
                state.last_error = str(error)[:255]

            threshold = self._failures_per_tier()
            # Escalate once per full group of consecutive failures.
            new_tier = state.consecutive_failures // threshold
            if new_tier > state.tier:
                state.tier = new_tier
                logger.warning(
                    f"Domain rate limiter: {state.domain} {state.consecutive_failures} "
                    f"consecutive failure(s), moving to tier {state.tier} "
                    f"(effective interval {state.effective_interval:.1f}s)"
                )
            self._release(state, now)

    def _release(self, state, now):
        """Free the serial slot and open the cooldown window. Called under lock."""
        state.in_flight = False
        state.in_flight_since = None
        # The next same-domain request may start no earlier than now + the
        # effective interval, so the spacing is measured end-to-start.
        state.window_ends_at = now + state.effective_interval

    def reset(self, domain=None):
        """Drop throttle state. With no argument, clears every domain."""
        with self._lock:
            if domain is None:
                self._states.clear()
            elif domain in self._states:
                del self._states[domain]

    def status_snapshot(self, datastore=None, limit=STATUS_SNAPSHOT_LIMIT):
        """Current per-domain state for the status API.

        Domains are ordered by (tier, failures) descending so the domains
        currently being throttled appear first.
        """
        with self._lock:
            states = list(self._states.values())

        enabled = True
        if datastore is not None:
            enabled = self._configured_min_interval(datastore) > 0

        states.sort(key=lambda s: (s.tier, s.consecutive_failures), reverse=True)
        items = [s.to_snapshot() for s in states[:limit]]
        return {
            'enabled': enabled,
            'domain_count': len(states),
            'domains': items,
        }


# Process-wide singleton — workers, the scheduler and the API all share it.
domain_rate_limiter = DomainRateLimiter()
