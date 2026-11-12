#!/usr/bin/env python3
"""The snapshot-retention thread's wiring: when it starts, when it doesn't, and that it stops.

The loop itself is covered by tests/unit/test_snapshot_retention.py; this only checks the
startup decision and shutdown behaviour in flask_app.
"""
import threading
import time

import changedetectionio.flask_app as flask_app


def _fake_app(exit_event):
    class FakeConfig:
        pass

    app = type('App', (), {'config': FakeConfig()})()
    app.config.exit = exit_event
    return app


def test_not_started_under_pytest(monkeypatch):
    exit_event = threading.Event()
    monkeypatch.setenv('SNAPSHOT_RETENTION_DISABLE_BACKGROUND', 'false')
    assert flask_app.start_snapshot_retention_thread(_fake_app(exit_event), object(), in_pytest=True) is None


def test_not_started_when_disabled(monkeypatch):
    exit_event = threading.Event()
    monkeypatch.setenv('SNAPSHOT_RETENTION_DISABLE_BACKGROUND', 'true')
    assert flask_app.start_snapshot_retention_thread(_fake_app(exit_event), object(), in_pytest=False) is None


def test_started_and_stops_on_exit(monkeypatch):
    exit_event = threading.Event()
    monkeypatch.setenv('SNAPSHOT_RETENTION_DISABLE_BACKGROUND', 'false')
    monkeypatch.setattr('changedetectionio.snapshot_retention.run_pass',
                        lambda *a, **k: {'marked': 0, 'deleted': 0, 'marked_watches': 0})
    monkeypatch.setattr('changedetectionio.snapshot_retention._env_int',
                        lambda name, default: 0)  # no initial delay, no wait between passes

    thread = flask_app.start_snapshot_retention_thread(_fake_app(exit_event), object(), in_pytest=False)
    assert thread is not None and thread.is_alive()
    # Let a pass or two run, then ask it to stop; supervise() must let a shutdown exit through
    time.sleep(0.2)
    exit_event.set()
    thread.join(timeout=5)
    assert not thread.is_alive(), "the retention thread must honour the shutdown event"
