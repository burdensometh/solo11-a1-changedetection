#!/usr/bin/env python3
"""A reply that is only the application shell must not be recorded as a change.

Some sites build the page with JavaScript and answer the plain HTTP client with the frame
only (`<body><div id="root"></div></body>` + scripts). Nothing there changes between checks,
so a watch looks healthy ("page fetched fine") while the real page is never seen, and a reply
that genuinely failed to render looks identical.

Behaviour under test:
  1. A plain-client check whose reply is an empty shell retries once with a browser.
     If the browser then renders the page, that content is what gets compared.
  2. If the browser reply is still empty, the check fails (last_error), no history is written
     and the previous snapshot is still the one shown.
"""
import os

import pytest
from flask import url_for

from .util import delete_all_watches, wait_for_all_checks


def _write(datastore_path, html):
    with open(os.path.join(datastore_path, "endpoint-content.txt"), "w") as f:
        f.write(html)


def _datastore(client):
    return client.application.config['DATASTORE']


def _add_watch(client, live_server, **extras):
    datastore = _datastore(client)
    # The same host the Flask test client is told to use, but pointed at the live server's bound address
    # (localhost.localdomain is only resolvable inside the CI container, not on a developer machine)
    url = live_server.url('/test-endpoint')
    uuid = datastore.add_watch(url=url, extras={'fetch_backend': 'html_requests', **extras})
    wait_for_all_checks(client)
    return datastore.data['watching'][uuid]


SHELL = ("<html><head><title>Shop</title><script src='/app.js'></script>"
         "<link rel='stylesheet' href='/a.css'></head>"
         "<body><div id='root'></div><noscript>Enable JavaScript</noscript></body></html>")

REAL_PAGE = ("<html><head><title>Shop</title></head><body>"
             "<h1>Widget</h1><p>Now in stock</p><p>Free delivery</p></body></html>")


@pytest.fixture
def fake_browser_fetcher(live_server, monkeypatch):
    """Stand in for the browser the empty-shell retry resolves to.

    Playwright/Chrome cannot run in this environment, so the retry's browser resolution
    (difference_detection_processor._browser_fetcher_choice) is pointed at a fetcher that
    "renders" whatever tests/render_browser-content.txt holds - what a Chrome container would
    put in the body after running the page's JavaScript. Only the retry path is replaced, so a
    normal fetch is still the real plain HTTP client against the test server.
    """
    from changedetectionio.content_fetchers.base import Fetcher
    from changedetectionio.processors.base import difference_detection_processor

    datastore_path = live_server.app.config['TEST_DATASTORE_PATH']
    render_file = os.path.join(datastore_path, 'render_browser-content.txt')
    calls = []

    class BrowserFetcher(Fetcher):
        fetcher_description = 'Fake browser fetcher (test)'
        supports_screenshots = True
        supports_xpath_element_data = True

        async def run(self, url=None, timeout=None, request_headers=None, **kwargs):
            calls.append({'url': url})
            with open(render_file, encoding='utf-8') as f:
                self.content = f.read()
            self.status_code = 200
            self.headers = {'content-type': 'text/html'}

        async def quit(self, watch=None):
            pass

        def get_error(self):
            return self.error

        def get_last_status_code(self):
            return self.status_code

        def screenshot_step(self, step_n):
            return None

        def is_ready(self):
            return True

    async def fake_choice(self, used_backend):
        return BrowserFetcher, 'html_fake_browser', None

    monkeypatch.setattr(difference_detection_processor, '_browser_fetcher_choice', fake_choice)
    try:
        yield {'calls': calls, 'render_file': render_file}
    finally:
        if os.path.isfile(render_file):
            os.unlink(render_file)


def test_empty_shell_retried_with_browser_and_content_compared(client, live_server, measure_memory_usage,
                                                               datastore_path, fake_browser_fetcher):
    # The plain client sees the shell, the browser renders the real page
    _write(datastore_path, SHELL)
    with open(fake_browser_fetcher['render_file'], 'w', encoding='utf-8') as f:
        f.write(REAL_PAGE)

    watch = _add_watch(client, live_server)
    assert not watch.get('last_error'), watch.get('last_error')
    assert watch.history_n == 1, "The browser-rendered page should have been recorded"
    assert 'Widget' in watch.get_history_snapshot(timestamp=list(watch.history.keys())[-1])
    # The browser was asked to retry the shell exactly once
    assert len(fake_browser_fetcher['calls']) == 1
    delete_all_watches(client)


def test_empty_shell_still_empty_is_a_failed_check_without_history(client, live_server, measure_memory_usage,
                                                                  datastore_path, fake_browser_fetcher):
    _write(datastore_path, SHELL)
    # The browser also gets the shell (site blocks the browser, or renders nothing)
    with open(fake_browser_fetcher['render_file'], 'w', encoding='utf-8') as f:
        f.write(SHELL)

    watch = _add_watch(client, live_server)
    assert watch.history_n == 0, "A still-empty shell must not be saved as history"
    assert watch.last_changed == 0
    assert 'empty page shell' in (watch.get('last_error') or '').lower()
    assert watch.get('last_check_status') == 200
    assert len(fake_browser_fetcher['calls']) == 1

    # ...and the same reply next time is not mistaken for a change either
    client.post(url_for("ui.form_watch_checknow"), follow_redirects=True)
    wait_for_all_checks(client)
    assert watch.history_n == 0
    assert 'empty page shell' in (watch.get('last_error') or '').lower()

    res = client.get(url_for("watchlist.index"))
    assert b'error-text' in res.data
    assert b'has-unread-changes' not in res.data
    delete_all_watches(client)


def test_real_short_page_is_not_treated_as_a_shell(client, live_server, measure_memory_usage, datastore_path,
                                                   fake_browser_fetcher):
    # A short but real page must be checked normally, no browser retry
    _write(datastore_path, '<html><body>cool it works</body></html>')
    with open(fake_browser_fetcher['render_file'], 'w', encoding='utf-8') as f:
        f.write(REAL_PAGE)

    watch = _add_watch(client, live_server)
    assert not watch.get('last_error'), watch.get('last_error')
    assert watch.history_n == 1
    assert not fake_browser_fetcher['calls'], "No browser retry for a page that has real text"
    delete_all_watches(client)


def test_shell_named_in_settings_is_a_change_when_empty_pages_are_a_change(client, live_server, measure_memory_usage,
                                                                          datastore_path, fake_browser_fetcher):
    """'Empty pages are a change' still records the shell as a change, no browser retry and no failure."""
    _write(datastore_path, SHELL)
    with open(fake_browser_fetcher['render_file'], 'w', encoding='utf-8') as f:
        f.write(SHELL)
    settings = _datastore(client).data['settings']['application']
    settings['empty_pages_are_a_change'] = True

    watch = _add_watch(client, live_server)
    assert not watch.get('last_error'), watch.get('last_error')
    assert watch.history_n == 1, "The user asked for empty pages to be recorded as a change"
    assert not fake_browser_fetcher['calls']
    delete_all_watches(client)
