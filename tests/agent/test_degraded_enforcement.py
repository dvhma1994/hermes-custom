"""Tests for tightened degraded-mode enforcement in LearningGovernance."""
from agent import learning_constants as lc
from agent.learning_governance import LearningGovernance
from agent.runtime_authority import AuthorityDecision
from agent.opval.store import OpvalStore


def _gov(tmp_path):
    store = OpvalStore(str(tmp_path / "deg.db"))
    return LearningGovernance(store._conn)


def test_degraded_allows_strict_policy_only(tmp_path):
    gov = _gov(tmp_path)
    gov._breaker.activate("safety")
    d = gov.enforce_policy("s", AuthorityDecision(policy=lc.AUTHORITY_POLICY_STRICT), "strict only")
    assert d.approved


def test_degraded_blocks_non_strict_policy(tmp_path):
    gov = _gov(tmp_path)
    gov._breaker.activate("safety")
    d = gov.enforce_policy("s", AuthorityDecision(policy=lc.AUTHORITY_POLICY_PERMISSIVE), "loosen")
    assert not d.approved


def test_degraded_blocks_strict_policy_with_extra_knob(tmp_path):
    """A strict policy must not smuggle a non-policy knob through in degraded mode."""
    gov = _gov(tmp_path)
    gov._breaker.activate("safety")
    d = gov.enforce_policy(
        "s",
        AuthorityDecision(policy=lc.AUTHORITY_POLICY_STRICT, tool_scope=("web_search",)),
        "strict + tool scope",
    )
    assert not d.approved


def test_healthy_mode_allows_compound_directive(tmp_path):
    """Outside degraded mode, a compound allowed-knob directive is fine."""
    gov = _gov(tmp_path)
    d = gov.enforce_policy(
        "s",
        AuthorityDecision(policy=lc.AUTHORITY_POLICY_STRICT, max_tool_iterations=10),
        "normal enforce",
    )
    assert d.approved
