"""
Per-watch fetch timeout policy.

A single global fetch timeout is a bad fit for a mix of slow and fast sites: the slow ones
get marked failed while the fast ones wait far longer than they need. This module keeps the
policy in one place so the worker, the processor and the tests all agree on:

* the *effective* timeout for a check - a per-watch override (``watch['timeout']``) on top of
  the system default, doubled once while a watch keeps timing out;
* what counts as a timeout, so every fetcher's timeout exception maps to the same
  :class:`WatchFetchTimeout`;
* the consecutive-timeout bookkeeping - three timeouts in a row double the watch's timeout
  exactly once and leave a note instead of doubling forever;
* the short requeue that makes a timeout a retryable failure.

The counters and the backoff live on the watch dict so they survive restarts. They are
internal state, not part of the public API contract (see
model/schema_utils.py:SYSTEM_MANAGED_NON_SPEC_FIELDS).
"""
import asyncio
import os

from loguru import logger

# Default fetch timeout in seconds when neither the watch nor the system settings give one.
DEFAULT_FETCH_TIMEOUT_SECONDS = 30

# Three consecutive timeouts before the watch's timeout is doubled.
TIMEOUT_BACKOFF_AFTER_CONSECUTIVE = 3

# A timeout is retried this many times in total (the original attempt plus this many requeues).
# Read lazily via _env_int() so tests/operators can override the env var without import-order effects.
DEFAULT_TIMEOUT_MAX_ATTEMPTS = 3
# Delay before a timed-out watch is requeued, so a wedged endpoint isn't hit in a tight loop.
DEFAULT_TIMEOUT_RETRY_DELAY_SECONDS = 2.0
# Cap on the retry delay (the delay grows a little on every attempt, see schedule_timeout_retry()).
MAX_TIMEOUT_RETRY_DELAY_SECONDS = 30.0


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _max_attempts():
    return max(1, _env_int('FETCH_TIMEOUT_MAX_ATTEMPTS', DEFAULT_TIMEOUT_MAX_ATTEMPTS))


def _retry_delay():
    return max(0.0, _env_float('FETCH_TIMEOUT_RETRY_DELAY_SECONDS', DEFAULT_TIMEOUT_RETRY_DELAY_SECONDS))


def _max_retry_delay():
    return max(0.0, _env_float('FETCH_TIMEOUT_MAX_RETRY_DELAY_SECONDS', MAX_TIMEOUT_RETRY_DELAY_SECONDS))

# Watch dict keys holding the policy state.
WATCH_TIMEOUT_FIELD = 'timeout'
CONSECUTIVE_TIMEOUTS_FIELD = 'consecutive_timeouts'
TIMEOUT_BACKOFF_ACTIVE_FIELD = 'timeout_backoff_active'
TIMEOUT_BACKOFF_NOTICE_FIELD = 'timeout_backoff_notice'

# Exception type names (and message fragments as a fallback) that mean "the fetch timed out".
_TIMEOUT_EXCEPTION_NAMES = frozenset({
    'WatchFetchTimeout',
    'BrowserFetchTimedOut',
    'TimeoutError',
    'ReadTimeout',
    'ConnectTimeout',
    'ReadTimeoutError',
    'ConnectTimeoutError',
    'PoolTimeout',
})
_TIMEOUT_MESSAGE_FRAGMENTS = (
    'timed out',
    'timeout',
)


class WatchFetchTimeout(Exception):
    """Raised when a fetch does not finish inside the watch's effective timeout.

    Carries the timeout (seconds) that was in force so the error text and the backoff logic
    can report the actual budget the fetch had.
    """

    def __init__(self, message, timeout_seconds=None, attempts=None):
        super().__init__(message)
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts


def is_timeout_exception(exc):
    """True when ``exc`` is a fetch timeout, whatever fetcher raised it.

    Covers the requests/urllib3 timeout family, asyncio/playwright TimeoutError, the browser
    fetchers' BrowserFetchTimedOut and our own WatchFetchTimeout, with a message-contains
    fallback for wrapped exceptions whose type name was lost.
    """
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in _TIMEOUT_EXCEPTION_NAMES:
            return True
        current = current.__cause__ or current.__context__

    text = str(exc).lower()
    return any(fragment in text for fragment in _TIMEOUT_MESSAGE_FRAGMENTS)


def get_base_timeout(watch, datastore):
    """The configured timeout for this watch before any backoff doubling: per-watch override
    when set to a positive number, otherwise the system-wide setting, then the built-in default."""
    watch_timeout = watch.get(WATCH_TIMEOUT_FIELD)
    try:
        if watch_timeout is not None and int(watch_timeout) > 0:
            return int(watch_timeout)
    except (TypeError, ValueError):
        logger.warning(f"Watch {watch.get('uuid')} has an invalid per-watch timeout "
                       f"{watch_timeout!r}, falling back to the system default")

    system_timeout = None
    if datastore is not None:
        system_timeout = datastore.data['settings']['requests'].get('timeout')
    try:
        if system_timeout is not None and int(system_timeout) > 0:
            return int(system_timeout)
    except (TypeError, ValueError):
        logger.warning(f"System requests timeout {system_timeout!r} is invalid, using the built-in default")

    return DEFAULT_FETCH_TIMEOUT_SECONDS


def get_effective_timeout(watch, datastore):
    """The timeout to hand the fetcher on the next check: the base timeout, doubled while the
    one-shot consecutive-timeout backoff is active."""
    base = get_base_timeout(watch, datastore)
    if watch.get(TIMEOUT_BACKOFF_ACTIVE_FIELD):
        return base * 2
    return base


def record_timeout(watch, datastore):
    """Bookkeeping after a check ended in a timeout.

    Bumps the consecutive-timeout counter and, on the third consecutive timeout, doubles the
    effective timeout once and records a human-readable note. The doubling only happens once:
    further timeouts keep failing with the doubled budget instead of growing it without bound.
    Any successful check resets this state (see :func:`reset_timeout_state`).

    Returns the note string when the backoff just triggered, otherwise None.
    """
    consecutive = int(watch.get(CONSECUTIVE_TIMEOUTS_FIELD) or 0) + 1
    update = {CONSECUTIVE_TIMEOUTS_FIELD: consecutive}
    notice = None

    if consecutive >= TIMEOUT_BACKOFF_AFTER_CONSECUTIVE and not watch.get(TIMEOUT_BACKOFF_ACTIVE_FIELD):
        update[TIMEOUT_BACKOFF_ACTIVE_FIELD] = True
        base = get_base_timeout(watch, datastore)
        effective = base * 2
        notice = (f"Fetch timed out {consecutive} times in a row - doubled this watch's fetch "
                  f"timeout from {base}s to {effective}s and will keep using it. "
                  f"Set a per-watch timeout in the watch's Request settings to change it.")
        update[TIMEOUT_BACKOFF_NOTICE_FIELD] = notice
        logger.warning(f"Watch {watch.get('uuid')} - {notice}")

    watch.update(update)
    return notice


def reset_timeout_state(watch):
    """Clear the timeout streak after a check that did not time out (success or another kind
    of failure). The doubled-timeout backoff and its note are dropped so the watch goes back to
    its configured timeout, and a later streak can trigger a fresh one-time doubling."""
    if not (watch.get(CONSECUTIVE_TIMEOUTS_FIELD)
            or watch.get(TIMEOUT_BACKOFF_ACTIVE_FIELD)
            or watch.get(TIMEOUT_BACKOFF_NOTICE_FIELD)):
        return
    watch.update({
        CONSECUTIVE_TIMEOUTS_FIELD: 0,
        TIMEOUT_BACKOFF_ACTIVE_FIELD: False,
        TIMEOUT_BACKOFF_NOTICE_FIELD: None,
    })


def get_timeout_backoff_notice(watch):
    """The persistent backoff note for this watch, or None."""
    return watch.get(TIMEOUT_BACKOFF_NOTICE_FIELD) or None


def get_timeout_attempts(item):
    """How many fetch attempts the queued item has already made (0 for a freshly scheduled item)."""
    if not isinstance(item, dict):
        return 0
    try:
        return max(0, int(item.get('timeout_attempts') or 0))
    except (TypeError, ValueError):
        return 0


def build_timeout_retry(uuid, priority, attempts):
    """The requeue decision for a timed-out fetch.

    Returns (delay_seconds, queued_item) when another attempt is allowed, or (None, None) once
    ``FETCH_TIMEOUT_MAX_ATTEMPTS`` has been reached. The delay backs off gently so an endpoint
    that stays slow isn't hammered, but stays well under the normal recheck interval.
    """
    if attempts + 1 >= _max_attempts():
        return None, None

    delay = min(
        _max_retry_delay(),
        _retry_delay() * (attempts + 1),
    )
    return delay, {'uuid': uuid, 'timeout_attempts': attempts + 1, 'priority': priority}


def schedule_timeout_retry(update_q, uuid, priority, attempts):
    """Requeue a timed-out watch for another attempt after a short delay.

    Timeouts are retryable failures: the watch goes back on the queue with the same priority
    class as a scheduled check, carrying the attempt counter. Must be called from the worker's
    event loop (it schedules a background task for the delay). Returns the queued item dict,
    or None when no retry is allowed.
    """
    delay, item = build_timeout_retry(uuid, priority, attempts)
    if item is None:
        return None

    from changedetectionio import worker_pool
    from changedetectionio.queuedWatchMetaData import PrioritizedItem

    async def _requeue():
        await asyncio.sleep(delay)
        worker_pool.queue_item_async_safe(
            update_q,
            PrioritizedItem(priority=item['priority'],
                            item={'uuid': item['uuid'], 'timeout_attempts': item['timeout_attempts']}),
            silent=True,
        )

    logger.info(f"Watch {uuid} - fetch timed out (attempt {attempts + 1}/{_max_attempts()}), "
                f"retrying in {delay:g}s")
    asyncio.create_task(_requeue())
    return item
