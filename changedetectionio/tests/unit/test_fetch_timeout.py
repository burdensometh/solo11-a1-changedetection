"""
Unit tests for the per-watch fetch timeout policy (changedetectionio/fetch_timeout.py).

Pure policy logic - no Flask app, no live server: effective-timeout resolution, the
three-strikes backoff (doubles exactly once and leaves a note), streak reset, timeout
exception classification, and the retry bookkeeping.
"""
import asyncio
import unittest
from unittest import mock

from changedetectionio import fetch_timeout
from changedetectionio.fetch_timeout import (
    DEFAULT_FETCH_TIMEOUT_SECONDS,
    TIMEOUT_BACKOFF_AFTER_CONSECUTIVE,
    WatchFetchTimeout,
    build_timeout_retry,
    get_base_timeout,
    get_effective_timeout,
    get_timeout_attempts,
    is_timeout_exception,
    record_timeout,
    reset_timeout_state,
    schedule_timeout_retry,
)


class FakeDatastore:
    def __init__(self, system_timeout=30):
        self.data = {'settings': {'requests': {'timeout': system_timeout}}}


class FakeWatch(dict):
    def __init__(self, **kw):
        super().__init__()
        self.update({
            'uuid': 'test-watch',
            'timeout': None,
            'consecutive_timeouts': 0,
            'timeout_backoff_active': False,
            'timeout_backoff_notice': None,
        })
        self.update(kw)


class EffectiveTimeoutTests(unittest.TestCase):

    def test_defaults_to_system_timeout_when_watch_has_no_override(self):
        ds = FakeDatastore(system_timeout=42)
        watch = FakeWatch()
        self.assertEqual(get_base_timeout(watch, ds), 42)
        self.assertEqual(get_effective_timeout(watch, ds), 42)

    def test_per_watch_override_wins_over_system_default(self):
        ds = FakeDatastore(system_timeout=30)
        watch = FakeWatch(timeout=90)
        self.assertEqual(get_base_timeout(watch, ds), 90)
        self.assertEqual(get_effective_timeout(watch, ds), 90)

    def test_builtin_default_when_nothing_configured(self):
        ds = FakeDatastore(system_timeout=None)
        watch = FakeWatch()
        self.assertEqual(get_base_timeout(watch, ds), DEFAULT_FETCH_TIMEOUT_SECONDS)
        self.assertEqual(DEFAULT_FETCH_TIMEOUT_SECONDS, 30)

    def test_invalid_watch_timeout_falls_back_to_system(self):
        ds = FakeDatastore(system_timeout=30)
        watch = FakeWatch(timeout='not-a-number')
        self.assertEqual(get_base_timeout(watch, ds), 30)
        watch = FakeWatch(timeout=0)
        self.assertEqual(get_base_timeout(watch, ds), 30)

    def test_backoff_doubles_effective_timeout_but_not_base(self):
        ds = FakeDatastore(system_timeout=30)
        watch = FakeWatch(timeout=10, timeout_backoff_active=True)
        self.assertEqual(get_base_timeout(watch, ds), 10)
        self.assertEqual(get_effective_timeout(watch, ds), 20)


class BackoffTests(unittest.TestCase):

    def test_three_consecutive_timeouts_double_once_and_note(self):
        ds = FakeDatastore(system_timeout=30)
        watch = FakeWatch(timeout=15)

        # First two timeouts: streak grows, no doubling yet.
        self.assertIsNone(record_timeout(watch, ds))
        self.assertEqual(watch['consecutive_timeouts'], 1)
        self.assertFalse(watch['timeout_backoff_active'])

        self.assertIsNone(record_timeout(watch, ds))
        self.assertEqual(watch['consecutive_timeouts'], 2)
        self.assertFalse(watch['timeout_backoff_active'])

        # Third timeout: doubling triggers exactly once, with a note.
        notice = record_timeout(watch, ds)
        self.assertEqual(watch['consecutive_timeouts'], 3)
        self.assertTrue(watch['timeout_backoff_active'])
        self.assertIsNotNone(notice)
        self.assertIn('15s to 30s', notice)
        self.assertEqual(watch['timeout_backoff_notice'], notice)
        self.assertEqual(get_effective_timeout(watch, ds), 30)

        # A fourth, fifth... timeout must NOT double again.
        next_notice = record_timeout(watch, ds)
        self.assertIsNone(next_notice)
        self.assertEqual(watch['consecutive_timeouts'], 4)
        self.assertTrue(watch['timeout_backoff_active'])
        self.assertEqual(get_effective_timeout(watch, ds), 30)

    def test_backoff_threshold_is_three(self):
        self.assertEqual(TIMEOUT_BACKOFF_AFTER_CONSECUTIVE, 3)

    def test_reset_clears_streak_backoff_and_note(self):
        ds = FakeDatastore(system_timeout=30)
        watch = FakeWatch(timeout=20)
        record_timeout(watch, ds)
        record_timeout(watch, ds)
        record_timeout(watch, ds)
        self.assertTrue(watch['timeout_backoff_active'])

        reset_timeout_state(watch)
        self.assertEqual(watch['consecutive_timeouts'], 0)
        self.assertFalse(watch['timeout_backoff_active'])
        self.assertIsNone(watch['timeout_backoff_notice'])
        # Back to the configured timeout, and a fresh streak can double again later.
        self.assertEqual(get_effective_timeout(watch, ds), 20)

        again = record_timeout(watch, ds)
        record_timeout(watch, ds)
        notice = record_timeout(watch, ds)
        self.assertIsNone(again)
        self.assertIsNotNone(notice)
        self.assertTrue(watch['timeout_backoff_active'])

    def test_reset_is_a_noop_on_a_clean_watch(self):
        watch = FakeWatch()
        reset_timeout_state(watch)
        self.assertEqual(watch['consecutive_timeouts'], 0)


class TimeoutClassificationTests(unittest.TestCase):

    def test_known_timeout_exception_types(self):
        from requests.exceptions import ConnectTimeout, ReadTimeout

        from changedetectionio.content_fetchers.exceptions import (
            BrowserFetchTimedOut,
        )
        from changedetectionio.content_fetchers.exceptions import (
            WatchFetchTimeout as FetcherWatchFetchTimeout,
        )

        for exc in (
            TimeoutError(),
            ReadTimeout("Read timed out"),
            ConnectTimeout("connection timed out"),
            BrowserFetchTimedOut("browser too slow"),
            FetcherWatchFetchTimeout("deadline exceeded", timeout_seconds=5),
            WatchFetchTimeout("deadline exceeded", timeout_seconds=5),
        ):
            self.assertTrue(is_timeout_exception(exc), f"{type(exc).__name__} should count as a timeout")

    def test_timeout_detected_through_exception_chaining(self):
        try:
            try:
                raise TimeoutError()
            except TimeoutError as inner:
                raise RuntimeError("wrapper") from inner
        except RuntimeError as outer:
            self.assertTrue(is_timeout_exception(outer))

    def test_message_fallback_for_wrapped_timeout(self):
        self.assertTrue(is_timeout_exception(RuntimeError("HTTPConnectionPool read timed out")))

    def test_non_timeout_exception_is_not_classified_as_timeout(self):
        self.assertFalse(is_timeout_exception(ValueError("bad filter")))
        self.assertFalse(is_timeout_exception(RuntimeError("connection refused")))


class RetryBookkeepingTests(unittest.TestCase):

    def test_attempt_counter_reads_queue_item(self):
        self.assertEqual(get_timeout_attempts({}), 0)
        self.assertEqual(get_timeout_attempts({'timeout_attempts': 2}), 2)
        self.assertEqual(get_timeout_attempts({'timeout_attempts': None}), 0)
        self.assertEqual(get_timeout_attempts(None), 0)

    def test_retry_decision_increments_attempts_and_caps_retries(self):
        with mock.patch.object(fetch_timeout, '_max_attempts', return_value=3), \
             mock.patch.object(fetch_timeout, '_retry_delay', return_value=2), \
             mock.patch.object(fetch_timeout, '_max_retry_delay', return_value=30):
            delay, item = build_timeout_retry('u1', priority=123, attempts=0)
            self.assertEqual(item, {'uuid': 'u1', 'timeout_attempts': 1, 'priority': 123})
            self.assertEqual(delay, 2)

            delay, item = build_timeout_retry('u1', priority=123, attempts=1)
            # Delay backs off (2s * (attempts + 1)).
            self.assertEqual(delay, 4)
            self.assertEqual(item['timeout_attempts'], 2)

            # Third attempt (attempts=2) is the last allowed - no further requeue.
            delay, item = build_timeout_retry('u1', priority=123, attempts=2)
            self.assertIsNone(delay)
            self.assertIsNone(item)

    def test_retry_delay_is_capped(self):
        with mock.patch.object(fetch_timeout, '_max_attempts', return_value=99), \
             mock.patch.object(fetch_timeout, '_retry_delay', return_value=20), \
             mock.patch.object(fetch_timeout, '_max_retry_delay', return_value=30):
            delay, _ = build_timeout_retry('u1', 1, attempts=0)
            self.assertEqual(delay, 20)
            delay, _ = build_timeout_retry('u1', 1, attempts=5)
            self.assertEqual(delay, 30)

    def test_schedule_requeues_through_worker_pool(self):
        from changedetectionio import worker_pool

        calls = []

        def fake_queue(q, item, silent=False):
            calls.append(item)
            return True

        async def scenario():
            # The patch must stay active until the delayed requeue task has actually run.
            with mock.patch.object(fetch_timeout, '_max_attempts', return_value=3), \
                 mock.patch.object(fetch_timeout, '_retry_delay', return_value=0), \
                 mock.patch.object(fetch_timeout, '_max_retry_delay', return_value=0), \
                 mock.patch.object(worker_pool, 'queue_item_async_safe', fake_queue):
                result = schedule_timeout_retry(update_q=mock.Mock(), uuid='u1', priority=123, attempts=0)
                # Yield control so the zero-delay requeue task is scheduled and completes.
                for _ in range(3):
                    await asyncio.sleep(0)
            return result

        result = asyncio.new_event_loop().run_until_complete(scenario())
        self.assertIsNotNone(result)
        self.assertEqual(result['timeout_attempts'], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].item, {'uuid': 'u1', 'timeout_attempts': 1})
        self.assertEqual(calls[0].priority, 123)

    def test_no_retry_after_last_attempt(self):
        with mock.patch.object(fetch_timeout, '_max_attempts', return_value=3):
            self.assertIsNone(schedule_timeout_retry(mock.Mock(), 'u1', 1, attempts=2))


if __name__ == '__main__':
    unittest.main()
