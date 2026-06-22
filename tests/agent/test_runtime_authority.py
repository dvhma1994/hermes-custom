"""Unit tests for agent/runtime_authority.py."""
import pytest
from unittest.mock import MagicMock

from agent import learning_constants as lc
from agent.runtime_authority import (
    AuthorityDecision,
    RuntimeAuthority,
    allowed_authority_knobs,
    apply_decision,
    hash_decision,
)


def test_default_authority_state():
    auth = RuntimeAuthority()
    assert auth.policy == lc.AUTHORITY_POLICY_PERMISSIVE
    assert auth.tool_scope is None
    assert auth.context_size_override is None
    assert auth.allow_self_delegate is True
    assert auth.max_tool_iterations == lc.DEFAULT_MAX_TOOL_ITERATIONS


def test_allowed_authority_knobs():
    assert set(allowed_authority_knobs()) == {
        "policy", "tool_scope", "context_size_override",
        "allow_self_delegate", "max_tool_iterations",
    }


def test_authority_decision_fields():
    decision = AuthorityDecision(
        policy=lc.AUTHORITY_POLICY_STRICT,
        tool_scope=frozenset(["web_search", "terminal"]),
        context_size_override=20000,
        allow_self_delegate=False,
        max_tool_iterations=5,
    )
    assert decision.policy == lc.AUTHORITY_POLICY_STRICT
    assert "terminal" in decision.tool_scope


def test_apply_decision_sets_agent_attributes():
    agent = MagicMock()
    agent._authority_policy = "permissive"
    agent._authority_tool_scope = None
    agent._authority_context_size_override = None
    agent._authority_delegation_policy = "permissive"
    agent._authority_max_tool_iterations = 50

    decision = AuthorityDecision(
        policy=lc.AUTHORITY_POLICY_STRICT,
        tool_scope=frozenset(["web_search"]),
        context_size_override=18000,
        allow_self_delegate=False,
        max_tool_iterations=10,
    )
    apply_decision(agent, decision)

    assert agent._authority_policy == lc.AUTHORITY_POLICY_STRICT
    assert agent._authority_tool_scope == frozenset(["web_search"])
    assert agent._authority_context_size_override == 18000
    assert agent._authority_delegation_policy == lc.AUTHORITY_POLICY_NO_SELF_DELEGATE
    assert agent._authority_max_tool_iterations == 10


def test_apply_decision_rejects_unknown_knob():
    agent = MagicMock()
    with pytest.raises(ValueError):
        apply_decision(agent, {"unknown_knob": 1})


def test_apply_decision_rejects_non_allowed_knob():
    # AuthorityDecision is frozen and only accepts known fields, so an unknown
    # knob cannot be constructed. We verify the dataclass boundary raises.
    with pytest.raises(TypeError):
        AuthorityDecision(unknown_knob=1)


def test_apply_decision_rejects_unknown_type():
    agent = MagicMock()
    with pytest.raises(ValueError):
        apply_decision(agent, {"policy": "strict"})


def test_hash_decision_is_deterministic():
    d1 = AuthorityDecision(policy="strict")
    d2 = AuthorityDecision(policy="strict")
    assert hash_decision(d1) == hash_decision(d2)


def test_authority_set_policy_validation():
    auth = RuntimeAuthority()
    auth.set_policy(lc.AUTHORITY_POLICY_STRICT)
    assert auth.policy == lc.AUTHORITY_POLICY_STRICT
    with pytest.raises(ValueError):
        auth.set_policy("invalid")


def test_authority_set_tool_scope_validation():
    auth = RuntimeAuthority()
    auth.set_tool_scope(["web_search", "terminal"])
    assert auth.tool_scope == frozenset(["web_search", "terminal"])
    with pytest.raises(ValueError):
        auth.set_tool_scope("not-a-list")


def test_authority_set_context_size_override():
    auth = RuntimeAuthority()
    auth.set_context_size_override(16000)
    assert auth.context_size_override == 16000
    with pytest.raises(ValueError):
        auth.set_context_size_override(-1)


def test_authority_set_allow_self_delegate():
    auth = RuntimeAuthority()
    auth.set_allow_self_delegate(False)
    assert auth.allow_self_delegate is False
    with pytest.raises(ValueError):
        auth.set_allow_self_delegate("yes")


def test_authority_set_max_tool_iterations():
    auth = RuntimeAuthority()
    auth.set_max_tool_iterations(3)
    assert auth.max_tool_iterations == 3
    with pytest.raises(ValueError):
        auth.set_max_tool_iterations(0)
