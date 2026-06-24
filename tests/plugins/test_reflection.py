"""Tests for the reflection plugin (Reflexion-style cross-turn failure nudge)."""
from __future__ import annotations

import pytest

from plugins.reflection import (
    _build_nudge,
    _looks_failed,
    _previous_turn_failures,
    _summarize_failure,
    reflection_hook,
    register,
)


# --- fixtures ---------------------------------------------------------------

def _user(text="do it"):
    return {"role": "user", "content": text}


def _failed_tool_msg(error="boom"):
    return {"role": "tool", "tool_call_id": "c1",
            "content": '{"success": false, "error": "%s"}' % error}


def _ok_tool_msg():
    return {"role": "tool", "tool_call_id": "c2",
            "content": '{"success": true, "result": "done"}'}


def _assistant_with_calls():
    return {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}


def _assistant_text(t="here you go"):
    return {"role": "assistant", "content": t}


# A realistic history: previous turn (user → assistant calls → FAILED tool →
# assistant text) then the current turn's new user message at the tail.
def _history_prev_turn_failed():
    return [
        _user("first request"),
        _assistant_with_calls(), _failed_tool_msg("connection refused"),
        _assistant_text("I hit an error"),
        _user("try again"),  # current turn
    ]


# --- flag gating ------------------------------------------------------------

def test_no_op_when_flag_unset(monkeypatch):
    monkeypatch.delenv("HERMES_REFLECTION", raising=False)
    out = reflection_hook(conversation_history=_history_prev_turn_failed(),
                          session_id="s", turn_id=1)
    assert out is None


def test_injects_on_prev_turn_failure_when_flag_on(monkeypatch):
    monkeypatch.setenv("HERMES_REFLECTION", "1")
    out = reflection_hook(conversation_history=_history_prev_turn_failed(),
                          session_id="s", turn_id=10)
    assert isinstance(out, dict)
    assert "Reflexion check" in out["context"]
    assert "connection refused" in out["context"]
    assert "ROOT CAUSE" in out["context"]


def test_no_injection_when_prev_turn_succeeded(monkeypatch):
    monkeypatch.setenv("HERMES_REFLECTION", "1")
    history = [
        _user("first"), _assistant_with_calls(), _ok_tool_msg(),
        _assistant_text("done"), _user("next"),
    ]
    assert reflection_hook(conversation_history=history, session_id="s", turn_id=11) is None


def test_first_turn_has_no_prior(monkeypatch):
    monkeypatch.setenv("HERMES_REFLECTION", "1")
    # Only one user message → no previous turn → nothing to reflect on.
    history = [_user("only one")]
    assert reflection_hook(conversation_history=history, session_id="s", turn_id=1) is None


def test_current_turn_failures_excluded(monkeypatch):
    monkeypatch.setenv("HERMES_REFLECTION", "1")
    # Failure AFTER the last user message = current turn → excluded (can't
    # re-inject mid-turn). No prior failure → no nudge.
    history = [
        _user("first"), _assistant_text("ok"),
        _user("current"), _assistant_with_calls(), _failed_tool_msg("late"),
    ]
    assert reflection_hook(conversation_history=history, session_id="s", turn_id=2) is None


# --- dedup ------------------------------------------------------------------

def test_dedup_same_turn(monkeypatch):
    monkeypatch.setenv("HERMES_REFLECTION", "1")
    h = _history_prev_turn_failed()
    first = reflection_hook(conversation_history=h, session_id="sess-d", turn_id=99)
    second = reflection_hook(conversation_history=h, session_id="sess-d", turn_id=99)
    assert isinstance(first, dict)
    assert second is None


def test_distinct_turns_each_inject(monkeypatch):
    monkeypatch.setenv("HERMES_REFLECTION", "1")
    h = _history_prev_turn_failed()
    a = reflection_hook(conversation_history=h, session_id="sess-x", turn_id=1)
    b = reflection_hook(conversation_history=h, session_id="sess-x", turn_id=2)
    assert isinstance(a, dict) and isinstance(b, dict)


# --- robustness -------------------------------------------------------------

def test_empty_and_malformed_history(monkeypatch):
    monkeypatch.setenv("HERMES_REFLECTION", "1")
    assert reflection_hook(conversation_history=[], session_id="s", turn_id=1) is None
    assert reflection_hook(conversation_history=None, session_id="s", turn_id=1) is None
    junk = [None, 42, {"role": "tool", "content": None}, "nope"]
    assert reflection_hook(conversation_history=junk, session_id="s", turn_id=2) is None


# --- helper units -----------------------------------------------------------

def test_looks_failed_detects_signatures():
    assert _looks_failed('{"success": false, "error": "x"}')
    assert _looks_failed('{"success":false}')
    assert _looks_failed("Traceback (most recent call last):\n  ...")
    assert not _looks_failed('{"success": true}')
    assert not _looks_failed("")


def test_summarize_extracts_error_and_clips():
    assert _summarize_failure('{"success": false, "error": "bad path"}') == "bad path"
    long = '{"success": false, "error": "%s"}' % ("x" * 500)
    assert len(_summarize_failure(long)) <= 181


def test_previous_turn_failures_orders_and_caps():
    msgs = [_user("a")]
    msgs += [_assistant_with_calls()] + [_failed_tool_msg(f"e{i}") for i in range(8)]
    msgs += [_assistant_text("done"), _user("b")]
    fails = _previous_turn_failures(msgs)
    assert 0 < len(fails) <= 4  # _MAX_FAILURES cap


def test_register_wires_pre_llm_call():
    captured = {}

    class _Ctx:
        def register_hook(self, name, cb):
            captured["name"] = name
            captured["cb"] = cb

    register(_Ctx())
    assert captured["name"] == "pre_llm_call"
    assert captured["cb"] is reflection_hook
