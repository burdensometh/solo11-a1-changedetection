"""Failure-aware retry/backoff for a watch check.

A check that fails is classified by how it failed, so the scheduler can decide when (or whether)
to try it again:

* ``timeout`` and ``server`` (HTTP 5xx) failures get retried with a 1s, 5s, 30s backoff, at most
  three times. The state is kept in the watch's ``retry`` metadata, which is persisted with the
  watch (so a restart carries on with the same backoff sequence) and is also what lets the
  scheduler re-queue the watch long before its normal time-between-check interval.
* ``permanent`` failures (HTTP 401/404) are recorded as permanent and never retried automatically -
  checking them again is left to the normal schedule or a manual "check now".
* Anything else is not retried by this mechanism.

The retry itself is queued by the scheduler (see flask_app.ticker_thread_check_time_launch_checks)
rather than slept off inside a worker, so a slow site never holds a fetch worker while waiting.
"""
import time

from loguru import logger

# Delays between the failed check and each retry. Three entries = at most three retries.
RETRY_DELAY_SECONDS = (1, 5, 30)

# HTTP statuses that mean "trying again will not help"
PERMANENT_FAILURE_STATUS_CODES = frozenset({401, 404})


def _iter_exception_chain(exception):
    """Yield the exception and its causes/contexts, guarding against cycles."""
    seen = set()
    while exception is not None and id(exception) not in seen:
        seen.add(id(exception))
        yield exception
        exception = exception.__cause__ or exception.__context__


def _is_timeout_error(exception):
    """True when any exception in the chain looks like a timeout.

    Covers requests' Timeout/ReadTimeout, asyncio.TimeoutError, playwright/selenium TimeoutError,
    urllib3 ReadTimeoutError, and fetchers that wrap them in a plain Exception (requests.py does
    ``raise Exception(msg) from e``); also message-based net::ERR_TIMED_OUT style failures. Some
    fetcher exceptions carry the text in a ``message``/``msg`` attribute instead of args
    (PageUnloadable, BrowserFetchTimedOut), so those are checked too.
    """
    for exc in _iter_exception_chain(exception):
        name = type(exc).__name__.lower()
        if 'timeout' in name or 'timedout' in name:
            return True
        texts = [str(exc), str(getattr(exc, 'message', '') or ''), str(getattr(exc, 'msg', '') or '')]
        for text in texts:
            if any(marker in text.lower() for marker in (
                    'timed out', 'timeout', 'err_timed_out', 'took too long',
                    'did not finish navigating', 'time limit exceeded')):
                return True
    return False


def classify_failure(exception):
    """(kind, status_code) for a failed check.

    kind is one of ``'permanent'`` (401/404, never retried), ``'server'`` (HTTP 5xx),
    ``'timeout'``, or ``None`` (not retried automatically).
    """
    status_code = getattr(exception, 'status_code', None)
    if isinstance(status_code, int):
        if status_code in PERMANENT_FAILURE_STATUS_CODES:
            return 'permanent', status_code
        if 500 <= status_code <= 599:
            return 'server', status_code
        return None, status_code

    if _is_timeout_error(exception):
        return 'timeout', status_code
    return None, status_code


def plan_retry(watch, exception, now=None):
    """The ``update_obj`` to store after a failed check, or None when nothing changes.

    Transient failures move the sequence on (1s -> 5s -> 30s -> stop); permanent and other
    failures clear any pending sequence so the scheduler cannot fire a stale retry.
    """
    now = time.time() if now is None else now
    kind, status_code = classify_failure(exception)
    current = watch.get('retry') or {}
    uuid = watch.get('uuid')

    if kind == 'permanent':
        logger.warning(f"Watch {uuid} - permanent failure (HTTP {status_code}), recorded as permanent, not retried")
        return {'retry': {'attempts': 0,
                          'check_started_at': watch.get('last_checked') or now,
                          'failed_at': now,
                          'kind': 'permanent',
                          'next_retry_at': None,
                          'status_code': status_code}}

    if kind in ('timeout', 'server'):
        attempts = int(current.get('attempts') or 0)
        if attempts < len(RETRY_DELAY_SECONDS):
            delay = RETRY_DELAY_SECONDS[attempts]
            logger.info(f"Watch {uuid} - {kind} failure"
                        f"{f' (HTTP {status_code})' if status_code else ''}, "
                        f"retry {attempts + 1}/{len(RETRY_DELAY_SECONDS)} in {delay}s")
            return {'retry': {'attempts': attempts + 1,
                              'check_started_at': watch.get('last_checked') or now,
                              'failed_at': now,
                              'kind': kind,
                              'next_retry_at': now + delay,
                              'status_code': status_code}}
        logger.warning(f"Watch {uuid} - {kind} failure, all {len(RETRY_DELAY_SECONDS)} retries used, "
                       f"leaving it to the normal recheck schedule")
        return {'retry': None}

    # Not a failure type we retry; drop a pending sequence from an earlier, different failure.
    return {'retry': None} if current else None

    # Not a failure type we retry; drop a pending sequence from an earlier, different failure.
    return {'retry': None} if current else None


def is_retry_due(watch, now=None):
    """True when the watch's scheduled retry has come due AND no check has started since the failure.

    The second condition is what stops a retry from being queued over and over: as soon as any
    check of the watch begins it stamps a newer ``last_checked``, which means the failure's retry
    was already acted on (or the user checked the watch manually). If the worker recorded the
    outcome, ``retry`` is replaced anyway - this only covers a check that died before it could.
    """
    now = time.time() if now is None else now
    entry = watch.get('retry') or {}
    next_retry_at = entry.get('next_retry_at')
    if not next_retry_at:
        return False
    try:
        if now < float(next_retry_at):
            return False
        return float(watch.get('last_checked') or 0) <= float(entry.get('check_started_at') or 0)
    except (TypeError, ValueError):
        return False
