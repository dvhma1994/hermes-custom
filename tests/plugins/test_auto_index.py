"""Tests for the auto_index plugin (on_session_start incremental backfill)."""
from __future__ import annotations

import threading

import plugins.auto_index as ai


def _reset_in_flight():
    with ai._lock:
        ai._in_flight = False


def test_flag_off_does_not_start(monkeypatch):
    monkeypatch.delenv("HERMES_AUTO_INDEX", raising=False)
    _reset_in_flight()
    assert ai._maybe_start() is False


def test_flag_on_starts_backfill_thread(monkeypatch):
    monkeypatch.setenv("HERMES_AUTO_INDEX", "1")
    _reset_in_flight()

    done = threading.Event()
    calls = []

    def fake_backfill(limit=ai._RECENT_LIMIT):
        calls.append(limit)
        with ai._lock:
            ai._in_flight = False
        done.set()
        return 0

    monkeypatch.setattr(ai, "_run_backfill", fake_backfill)
    assert ai._maybe_start() is True
    assert done.wait(timeout=5)
    assert calls == [ai._RECENT_LIMIT]


def test_single_flight_skips_second(monkeypatch):
    monkeypatch.setenv("HERMES_AUTO_INDEX", "1")
    _reset_in_flight()
    with ai._lock:
        ai._in_flight = True  # simulate a backfill already running
    try:
        assert ai._maybe_start() is False
    finally:
        _reset_in_flight()


def test_run_backfill_noop_without_semantic_recall(monkeypatch):
    # No HERMES_SEMANTIC_RECALL → is_enabled() False → clean 0, no DB/embedder touched.
    monkeypatch.delenv("HERMES_SEMANTIC_RECALL", raising=False)
    _reset_in_flight()
    assert ai._run_backfill(5) == 0


def test_session_start_hook_returns_none(monkeypatch):
    monkeypatch.delenv("HERMES_AUTO_INDEX", raising=False)
    _reset_in_flight()
    assert ai.session_start_hook(session_id="s", model="m", platform="cli") is None


def test_register_wires_on_session_start():
    captured = {}

    class _Ctx:
        def register_hook(self, name, cb):
            captured["name"] = name
            captured["cb"] = cb

    ai.register(_Ctx())
    assert captured["name"] == "on_session_start"
    assert captured["cb"] is ai.session_start_hook
