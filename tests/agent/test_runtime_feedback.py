"""Unit tests for agent/runtime_feedback.py."""
import pytest
from dataclasses import fields
from unittest.mock import MagicMock

from agent import learning_constants as lc
from agent.runtime_authority import AuthorityDecision, RuntimeAuthority
from agent.runtime_feedback import RuntimeFeedbackCollector


def test_collector_initially_empty():
    collector = RuntimeFeedbackCollector()
    assert collector.events == []


def test_record_tool_outcome():
    collector = RuntimeFeedbackCollector()
    collector.record_tool_outcome({
        "tool_name": "web_search",
        "outcome": lc.FEEDBACK_OUTCOME_SUCCESS,
    })
    assert len(collector.events) == 1
    assert collector.events[0]["tool_name"] == "web_search"


def test_record_tool_outcome_enforces_required_fields():
    collector = RuntimeFeedbackCollector()
    with pytest.raises(ValueError):
        collector.record_tool_outcome({"tool_name": "web_search"})
    with pytest.raises(ValueError):
        collector.record_tool_outcome({"outcome": "success"})


def test_record_tool_outcome_rejects_bad_outcome():
    collector = RuntimeFeedbackCollector()
    with pytest.raises(ValueError):
        collector.record_tool_outcome({
            "tool_name": "web_search",
            "outcome": "bad",
        })


def test_record_delegate_outcome():
    collector = RuntimeFeedbackCollector()
    collector.record_delegate_outcome({
        "tool_name": "delegate_task",
        "outcome": lc.FEEDBACK_OUTCOME_FAILURE,
        "task_count": 2,
    })
    assert collector.events[0]["event_type"] == "delegate"


def test_record_delegate_outcome_requires_task_count():
    collector = RuntimeFeedbackCollector()
    with pytest.raises(ValueError):
        collector.record_delegate_outcome({
            "tool_name": "delegate_task",
            "outcome": "success",
        })


def test_event_limit():
    collector = RuntimeFeedbackCollector()
    for i in range(lc.MAX_FEEDBACK_EVENTS_PER_TURN + 5):
        collector.record_tool_outcome({
            "tool_name": "t",
            "outcome": lc.FEEDBACK_OUTCOME_SUCCESS,
        })
    assert len(collector.events) == lc.MAX_FEEDBACK_EVENTS_PER_TURN


def test_adapt_no_events():
    authority = RuntimeAuthority()
    collector = RuntimeFeedbackCollector()
    assert collector.adapt(authority) is None


def test_adapt_failure_rate_triggers_strict_policy():
    authority = RuntimeAuthority()
    collector = RuntimeFeedbackCollector()
    for _ in range(10):
        collector.record_tool_outcome({
            "tool_name": "terminal",
            "outcome": lc.FEEDBACK_OUTCOME_FAILURE,
        })
    decision = collector.adapt(authority)
    assert decision is not None
    assert decision.policy == lc.AUTHORITY_POLICY_STRICT


def test_adapt_does_not_exceed_allowed_knobs():
    authority = RuntimeAuthority()
    collector = RuntimeFeedbackCollector()
    for _ in range(20):
        collector.record_tool_outcome({
            "tool_name": "web_search",
            "outcome": lc.FEEDBACK_OUTCOME_FAILURE,
        })
    decision = collector.adapt(authority)
    for field_obj in fields(decision):
        assert field_obj.name in {
            "policy", "tool_scope", "context_size_override",
            "allow_self_delegate", "max_tool_iterations",
        }


def test_adapt_records_decision_hash():
    authority = RuntimeAuthority()
    collector = RuntimeFeedbackCollector()
    collector.record_tool_outcome({
        "tool_name": "terminal",
        "outcome": lc.FEEDBACK_OUTCOME_FAILURE,
    })
    collector.record_tool_outcome({
        "tool_name": "terminal",
        "outcome": lc.FEEDBACK_OUTCOME_FAILURE,
    })
    collector.adapt(authority)
    assert collector.last_decision_hash is not None


def test_adapt_idempotent_same_hash():
    authority = RuntimeAuthority()
    collector = RuntimeFeedbackCollector()
    for _ in range(5):
        collector.record_tool_outcome({
            "tool_name": "terminal",
            "outcome": lc.FEEDBACK_OUTCOME_FAILURE,
        })
    d1 = collector.adapt(authority)
    h1 = collector.last_decision_hash
    d2 = collector.adapt(authority)
    h2 = collector.last_decision_hash
    assert h1 == h2


def test_evidence_ownership():
    collector = RuntimeFeedbackCollector()
    assert collector.source_module == "agent.runtime_feedback"
    assert collector.owner == "learning-governance-v1"


def test_max_events_per_turn_constant():
    assert lc.MAX_FEEDBACK_EVENTS_PER_TURN == 100
