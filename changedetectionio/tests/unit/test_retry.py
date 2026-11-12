#!/usr/bin/env python3

# run from dir above changedetectionio/ dir
# python3 -m unittest changedetectionio.tests.unit.test_retry

import unittest

from changedetectionio import retry


class FakeWatch(dict):
    """Just enough of a watch: .get() and ['retry'] are all retry.py needs."""

    def __init__(self, **kw):
        super().__init__({'uuid': 'test-uuid', 'last_checked': 1000, 'retry': None, **kw})


class TestClassifyFailure(unittest.TestCase):

    def test_404_and_401_are_permanent(self):
        class Non200Error(Exception):
            def __init__(self, status_code):
                self.status_code = status_code

        for status_code in (401, 404):
            kind, code = retry.classify_failure(Non200Error(status_code))
            self.assertEqual(kind, 'permanent', f"HTTP {status_code} must be a permanent failure")
            self.assertEqual(code, status_code)

    def test_5xx_is_server_failure(self):
        class Non200Error(Exception):
            def __init__(self, status_code):
                self.status_code = status_code

        for status_code in (500, 502, 503, 504, 599):
            kind, code = retry.classify_failure(Non200Error(status_code))
            self.assertEqual(kind, 'server', f"HTTP {status_code} must be a retryable server failure")

    def test_other_status_codes_are_not_retried(self):
        class Non200Error(Exception):
            def __init__(self, status_code):
                self.status_code = status_code

        for status_code in (200, 400, 403, 429):
            kind, _code = retry.classify_failure(Non200Error(status_code))
            self.assertIsNone(kind, f"HTTP {status_code} is not in the retry policy")

    def test_timeout_exception_types(self):
        self.assertEqual(retry.classify_failure(TimeoutError())[0], 'timeout')
        # requests.ReadTimeout / Timeout, playwright TimeoutError all share "Timeout" in the name
        self.assertEqual(retry.classify_failure(type('Timeout', (Exception,), {})())[0], 'timeout')
        self.assertEqual(retry.classify_failure(type('TimeoutError', (Exception,), {})())[0], 'timeout')

    def test_timeout_wrapped_in_a_plain_exception(self):
        # requests.py does `raise Exception(msg) from e`, so the cause chain must be checked
        try:
            try:
                raise TimeoutError('Read timed out. (read timeout=5)')
            except TimeoutError as e:
                raise Exception('outer wrap') from e
        except Exception as wrapped:
            self.assertEqual(retry.classify_failure(wrapped)[0], 'timeout')

    def test_message_only_timeout(self):
        self.assertEqual(retry.classify_failure(Exception('net::ERR_TIMED_OUT'))[0], 'timeout')
        self.assertEqual(retry.classify_failure(Exception('Browser processing took too long'))[0], 'timeout')

    def test_fetcher_exception_with_message_attribute(self):
        # PageUnloadable / BrowserFetchTimedOut keep the text in .message/.msg, not in args
        class FetcherError(Exception):
            def __init__(self, message, status_code=None):
                self.message = message
                self.status_code = status_code

        self.assertEqual(retry.classify_failure(FetcherError('Timeout 30000ms exceeded.'))[0], 'timeout')
        self.assertEqual(retry.classify_failure(FetcherError('Browser did not finish navigating within 30s'))[0],
                         'timeout')

    def test_5xx_fetcher_exception_is_a_server_failure(self):
        class FetcherError(Exception):
            def __init__(self, message, status_code):
                self.message = message
                self.status_code = status_code

        self.assertEqual(retry.classify_failure(FetcherError('Gateway timeout', status_code=504)),
                         ('server', 504), "A status code wins over the message text")

    def test_unrelated_error_is_not_retried(self):
        self.assertIsNone(retry.classify_failure(Exception('Sensor error'))[0])
        self.assertIsNone(retry.classify_failure(ValueError('bad value'))[0])


class TestPlanRetry(unittest.TestCase):

    def _non200_error(self, status_code):
        class Non200Error(Exception):
            def __init__(self, status_code):
                self.status_code = status_code
        return Non200Error(status_code)

    def test_timeout_backoff_is_1_5_30_seconds(self):
        watch = FakeWatch()
        now = 1000
        for attempt, delay in enumerate((1, 5, 30), start=1):
            update = retry.plan_retry(watch, TimeoutError('timed out'), now=now)
            entry = update['retry']
            self.assertEqual(entry['attempts'], attempt)
            self.assertEqual(entry['kind'], 'timeout')
            self.assertEqual(entry['next_retry_at'], now + delay,
                             f"attempt {attempt} must wait {delay}s")
            watch['retry'] = entry
            now = entry['next_retry_at']

    def test_5xx_follows_the_same_backoff(self):
        watch = FakeWatch()
        now = 1000
        for attempt, delay in enumerate((1, 5, 30), start=1):
            update = retry.plan_retry(watch, self._non200_error(503), now=now)
            entry = update['retry']
            self.assertEqual(entry['attempts'], attempt)
            self.assertEqual(entry['kind'], 'server')
            self.assertEqual(entry['status_code'], 503)
            self.assertEqual(entry['next_retry_at'], now + delay)
            watch['retry'] = entry
            now = entry['next_retry_at']

    def test_gives_up_after_three_retries(self):
        watch = FakeWatch()
        now = 1000
        for _ in range(3):
            watch['retry'] = retry.plan_retry(watch, self._non200_error(500), now=now)['retry']
            now = watch['retry']['next_retry_at']
        # The fourth failure is not retried any more: the state is cleared so the scheduler
        # falls back to the normal time-between-check schedule.
        update = retry.plan_retry(watch, self._non200_error(500), now=now)
        self.assertEqual(update, {'retry': None})
        watch.update(update)
        self.assertFalse(retry.is_retry_due(watch, now=now + 10000))

    def test_permanent_failure_has_no_next_retry(self):
        watch = FakeWatch()
        update = retry.plan_retry(watch, self._non200_error(404), now=1000)
        entry = update['retry']
        self.assertEqual(entry['kind'], 'permanent')
        self.assertIsNone(entry['next_retry_at'])
        self.assertEqual(entry['status_code'], 404)
        self.assertFalse(retry.is_retry_due(watch, now=1000 + 3600))

    def test_401_is_permanent_too(self):
        update = retry.plan_retry(FakeWatch(), self._non200_error(401), now=1000)
        self.assertEqual(update['retry']['kind'], 'permanent')

    def test_success_resets_the_sequence(self):
        # A later 500 after a success starts again from the 1s delay
        watch = FakeWatch(retry=None)
        update = retry.plan_retry(watch, self._non200_error(500), now=2000)
        self.assertEqual(update['retry']['attempts'], 1, "a success clears retry, so this starts over")
        self.assertEqual(update['retry']['next_retry_at'], 2001)

    def test_the_sequence_continues_across_failures(self):
        watch = FakeWatch(retry={'attempts': 1, 'kind': 'server', 'next_retry_at': 1010})
        update = retry.plan_retry(watch, self._non200_error(500), now=2000)
        self.assertEqual(update['retry']['attempts'], 2, "continues the sequence")
        self.assertEqual(update['retry']['next_retry_at'], 2005)

    def test_unrelated_failure_clears_a_pending_retry(self):
        watch = FakeWatch(retry={'attempts': 1, 'kind': 'timeout', 'next_retry_at': 1005})
        update = retry.plan_retry(watch, ValueError('bad filters'), now=1001)
        self.assertEqual(update, {'retry': None})

    def test_unrelated_failure_does_not_write_when_nothing_pending(self):
        self.assertIsNone(retry.plan_retry(FakeWatch(), ValueError('bad filters'), now=1001))


class TestIsRetryDue(unittest.TestCase):

    def test_not_due_before_the_time(self):
        watch = FakeWatch(last_checked=1000,
                          retry={'attempts': 1, 'check_started_at': 1000, 'next_retry_at': 1005})
        self.assertFalse(retry.is_retry_due(watch, now=1004))

    def test_due_at_and_after_the_time(self):
        watch = FakeWatch(last_checked=1000,
                          retry={'attempts': 1, 'check_started_at': 1000, 'next_retry_at': 1005})
        self.assertTrue(retry.is_retry_due(watch, now=1005))
        self.assertTrue(retry.is_retry_due(watch, now=1010))

    def test_not_due_once_a_check_has_started_since(self):
        # The retry was queued and taken (the worker stamped a newer last_checked), but the
        # failure state has not been rewritten yet: do not queue it again.
        watch = FakeWatch(last_checked=1006,
                          retry={'attempts': 1, 'check_started_at': 1000, 'next_retry_at': 1005})
        self.assertFalse(retry.is_retry_due(watch, now=1007))

    def test_no_retry_state_is_never_due(self):
        self.assertFalse(retry.is_retry_due(FakeWatch(retry=None), now=99999))
        self.assertFalse(retry.is_retry_due(FakeWatch(retry={'kind': 'permanent', 'next_retry_at': None}), now=99999))


class TestPersistence(unittest.TestCase):
    """The backoff state lives in the watch dict, so it is written to watch.json and read back."""

    def test_retry_state_survives_a_commit_and_reload(self):
        import json
        import os
        import tempfile

        from changedetectionio.model.Watch import model

        mock_datastore = {'settings': {'application': {}}, 'watching': {}}
        with tempfile.TemporaryDirectory() as datastore_path:
            watch = model(datastore_path=datastore_path, __datastore=mock_datastore, default={})
            watch['url'] = 'http://example.com'
            watch['retry'] = {'attempts': 2, 'check_started_at': 1000, 'failed_at': 1001,
                              'kind': 'timeout', 'next_retry_at': 1031, 'status_code': None}
            watch.commit()

            with open(os.path.join(watch.data_dir, 'watch.json')) as f:
                on_disk = json.load(f)
            self.assertEqual(on_disk['retry']['attempts'], 2)
            self.assertEqual(on_disk['retry']['next_retry_at'], 1031)

            # A restart rehydrates the same state, so the next retry continues the sequence
            reloaded = model(datastore_path=datastore_path, __datastore=mock_datastore,
                                  default=on_disk)
            self.assertEqual(reloaded['retry'], on_disk['retry'])


if __name__ == '__main__':
    unittest.main()
