"""Integration/adversarial tests for Milestone 1 hooks."""
import pytest
from unittest.mock import MagicMock, patch

from agent import learning_constants as lc
from agent.runtime_authority import AuthorityDecision, RuntimeAuthority, apply_decision
from agent.runtime_feedback import RuntimeFeedbackCollector


class FakeToolCall:
    def __init__(self, name):
        self.function = MagicMock()
        self.function.name = name


def test_cap_delegate_task_calls_no_self_delegate():
    from run_agent import AIAgent
    agent = MagicMock()
    agent._authority_delegation_policy = lc.AUTHORITY_POLICY_NO_SELF_DELEGATE
    tcs = [FakeToolCall("web_search"), FakeToolCall("delegate_task"), FakeToolCall("delegate_task")]
    out = AIAgent._cap_delegate_task_calls(tcs, agent)
    assert len(out) == 1
    assert out[0].function.name == "web_search"


def test_cap_delegate_task_calls_legacy_unchanged():
    from run_agent import AIAgent
    tcs = [FakeToolCall("web_search")]
    out = AIAgent._cap_delegate_task_calls(tcs, None)
    assert out == tcs


def test_apply_decision_preserves_other_agent_attributes():
    agent = MagicMock()
    agent._authority_policy = "permissive"
    agent._some_unrelated_attr = "must_preserve"
    decision = AuthorityDecision(policy=lc.AUTHORITY_POLICY_STRICT)
    apply_decision(agent, decision)
    assert agent._some_unrelated_attr == "must_preserve"


def test_tool_executor_records_outcome():
    from agent import tool_executor as te
    agent = MagicMock()
    collector = RuntimeFeedbackCollector()
    agent._runtime_feedback = collector
    te._record_tool_outcome_safe(agent, "web_search", False)
    assert len(collector.events) == 1
    assert collector.events[0]["outcome"] == "success"


def test_tool_executor_swallows_feedback_exception():
    from agent import tool_executor as te
    agent = MagicMock()
    agent._runtime_feedback = object()  # invalid type
    # Should not raise
    te._record_tool_outcome_safe(agent, "web_search", False)


def test_delegate_tool_records_outcome():
    from tools import delegate_tool as dt
    parent = MagicMock()
    collector = RuntimeFeedbackCollector()
    parent._runtime_feedback = collector
    dt.record_delegate_outcome_safe(parent, [{"status": "completed"}, {"status": "error"}])
    assert collector.events[0]["outcome"] == "failure"


def test_authority_tool_scope_filtering():
    from agent import chat_completion_helpers as cch
    agent = MagicMock()
    agent._authority_tool_scope = frozenset(["web_search"])
    tools = [
        {"function": {"name": "web_search"}},
        {"function": {"name": "terminal"}},
    ]
    out = cch._maybe_filter_tools_for_authority(agent, tools)
    assert len(out) == 1
    assert out[0]["function"]["name"] == "web_search"


def test_authority_tool_scope_none_passes_through():
    from agent import chat_completion_helpers as cch
    agent = MagicMock()
    agent._authority_tool_scope = None
    tools = [{"function": {"name": "terminal"}}]
    assert cch._maybe_filter_tools_for_authority(agent, tools) == tools


def test_runtime_authority_hash_chain_stability():
    from agent.runtime_authority import hash_decision
    d = AuthorityDecision(
        policy="strict",
        tool_scope=frozenset(["a", "b"]),
    )
    h1 = hash_decision(d)
    h2 = hash_decision(d)
    assert h1 == h2
    assert isinstance(h1, str) and len(h1) == 64


def test_adapt_respects_min_sessions_constant():
    assert lc.LEARNING_PROMOTION_MIN_REAL_SESSIONS == 50


def test_feedback_no_persistence_or_schema_changes():
    """Milestone 1 must not touch state.db or create persistence files."""
    collector = RuntimeFeedbackCollector()
    assert collector.events is not None
    assert not hasattr(collector, "_db_conn")
