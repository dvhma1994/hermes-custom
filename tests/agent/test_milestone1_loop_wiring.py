"""E2E wiring tests for the Milestone 1 turn-start authority/feedback hook.

These cover the gap the unit tests miss: the *conversation-loop integration*
of ``ensure_runtime_authority`` / ``ensure_runtime_feedback``. Before this was
fixed, ``agent/conversation_loop.py`` imported two functions that did not
exist, so the ``ImportError`` was swallowed every turn and the whole reactive
authority path was inert (dead reads on never-set attributes).

The tests assert, without a live model:
  1. the exact imports the loop performs (``conversation_loop.py:592-593``) resolve;
  2. the turn-start hook wires the attributes downstream loop code reads;
  3. per-turn reset prevents a previous turn's tightening from leaking;
  4. the full reactive chain (record -> adapt -> apply -> delegate cap) works;
  5. the context-size attribute name is reconciled with the loop read site;
  6. the hook never sets cache-affecting knobs (sacred prompt-cache invariant).
"""
from types import SimpleNamespace

import pytest

from agent import learning_constants as lc


def test_loop_imports_resolve():
    # Mirror conversation_loop.py:592-593 exactly. If these raise ImportError,
    # the turn-start hook is dead (the original bug).
    from agent.runtime_authority import ensure_runtime_authority  # noqa: F401
    from agent.runtime_feedback import ensure_runtime_feedback  # noqa: F401


def test_turn_start_hook_wires_attributes():
    from agent.runtime_authority import RuntimeAuthority, ensure_runtime_authority
    from agent.runtime_feedback import RuntimeFeedbackCollector, ensure_runtime_feedback

    agent = SimpleNamespace()
    ensure_runtime_authority(agent)
    ensure_runtime_feedback(agent)

    assert isinstance(agent._runtime_authority, RuntimeAuthority)
    assert isinstance(agent._runtime_feedback, RuntimeFeedbackCollector)
    # Downstream loop code (run_agent._cap_delegate_task_calls) reads these.
    assert agent._authority_policy == lc.AUTHORITY_POLICY_PERMISSIVE
    assert agent._authority_delegation_policy == lc.AUTHORITY_POLICY_PERMISSIVE
    assert agent._authority_allow_self_delegate is True
    assert agent._runtime_feedback.events == []


def test_per_turn_reset_clears_tightening_and_events():
    from agent.runtime_authority import apply_decision, ensure_runtime_authority
    from agent.runtime_feedback import ensure_runtime_feedback

    agent = SimpleNamespace()
    ensure_runtime_authority(agent)
    fb = ensure_runtime_feedback(agent)

    # Heavy-failure turn tightens delegation for the rest of the turn.
    for _ in range(4):
        fb.record_tool_outcome({"tool_name": "terminal", "outcome": lc.FEEDBACK_OUTCOME_FAILURE})
    decision = fb.adapt(agent._runtime_authority)
    assert decision is not None
    apply_decision(agent, decision)
    assert agent._authority_delegation_policy == lc.AUTHORITY_POLICY_NO_SELF_DELEGATE

    # Next turn: the hook resets to permissive defaults; nothing leaks.
    ensure_runtime_authority(agent)
    ensure_runtime_feedback(agent)
    assert agent._authority_delegation_policy == lc.AUTHORITY_POLICY_PERMISSIVE
    assert agent._authority_allow_self_delegate is True
    assert agent._runtime_feedback.events == []


def test_full_reactive_chain_caps_self_delegation():
    """record (via tool_executor) -> adapt -> apply -> _cap_delegate_task_calls."""
    from run_agent import AIAgent
    from agent import tool_executor as te
    from agent.runtime_authority import apply_decision, ensure_runtime_authority
    from agent.runtime_feedback import ensure_runtime_feedback

    class _FakeToolCall:
        def __init__(self, name):
            self.function = SimpleNamespace(name=name)

    agent = SimpleNamespace()
    ensure_runtime_authority(agent)
    ensure_runtime_feedback(agent)

    # Two failing tools in the turn (failure_rate 1.0, total >= 2) -> strict.
    te._record_tool_outcome_safe(agent, "terminal", True)
    te._record_tool_outcome_safe(agent, "terminal", True)
    decision = agent._runtime_feedback.adapt(agent._runtime_authority)
    assert decision is not None and decision.policy == lc.AUTHORITY_POLICY_STRICT
    apply_decision(agent, decision)

    calls = [_FakeToolCall("web_search"), _FakeToolCall("delegate_task")]
    out = AIAgent._cap_delegate_task_calls(calls, agent)
    assert [c.function.name for c in out] == ["web_search"]


def test_context_size_attr_name_reconciled():
    """apply_decision must write the same attribute the loop reads.

    The loop reads ``_authority_context_size_override`` (conversation_loop.py).
    A regression that renames either side silently makes the knob a no-op.
    """
    from agent.runtime_authority import AuthorityDecision, apply_decision

    agent = SimpleNamespace()
    apply_decision(agent, AuthorityDecision(context_size_override=18000))
    assert getattr(agent, "_authority_context_size_override", None) == 18000


def test_hook_never_sets_cache_affecting_knobs():
    """Sacred invariant: the turn-start hook must not touch knobs that mutate
    the byte-stable system prompt or the cached tool list."""
    from agent.runtime_authority import ensure_runtime_authority
    from agent.runtime_feedback import ensure_runtime_feedback

    agent = SimpleNamespace()
    ensure_runtime_authority(agent)
    ensure_runtime_feedback(agent)

    for attr in (
        "_authority_tool_scope",
        "_authority_context_size_override",
        "_authority_tool_guidance",
        "_authority_system_prompt_suffix",
    ):
        assert not hasattr(agent, attr), f"hook must not set cache-affecting knob {attr}"
