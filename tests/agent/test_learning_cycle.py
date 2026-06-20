"""End-to-end tests for the offline learning cycle (agent/learning_cycle.py).

These exercise the *real* pipeline against a temp OPVAL store: seed OPVAL
sessions/turns -> run_learning_cycle -> a governance decision is recorded ->
apply_learned_authority pushes only the cache-safe knobs onto a live agent.

This is the integration the AGENTS.md E2E bar requires: it drives the learning
modules through the same path production would, not in isolated unit mocks.
"""
import uuid
from types import SimpleNamespace

import pytest

from agent import learning_constants as lc
from agent import learning_cycle as lcyc
from agent.opval.store import OpvalStore


def _make_store(tmp_path):
    return OpvalStore(str(tmp_path / "cycle.db"))


def _seed_session(
    store,
    session_id,
    *,
    outcome="success",
    score=0.92,
    drift=0.04,
    misalignment=0.04,
    domain="coding",
    turn_count=3,
    tool_execution_count=2,
    synthetic=0,
    start_time=10.0,
):
    store.insert_session({
        "session_id": session_id,
        "task_id": "test",
        "parent_session_id": None,
        "root_session_id": session_id,
        "platform": "test",
        "primary_domain": domain,
        "secondary_domains": "[]",
        "start_time": start_time,
        "end_time": start_time + 1.0,
        "turn_count": turn_count,
        "tool_execution_count": tool_execution_count,
        "outcome": outcome,
        "session_quality_score": score,
        "session_tool_correctness": score,
        "drift_pct": drift,
        "misalignment_pct": misalignment,
        "promotion_score": score,
        "synthetic": synthetic,
        "synthetic_reason": None,
        "opval_enabled": 1,
        "recorded_at": start_time + 1.0,
    })
    store.insert_turn({
        "turn_id": str(uuid.uuid4()),
        "session_id": session_id,
        "turn_number": 1,
        "outcome": outcome,
        "tool_calls": "[]",
        "tool_outputs": '["ok"]',
        "error_class": None,
        "latency_ms": 100,
        "tokens_used": 50,
        "quality_score": score,
        "tool_execution_score": score,
        "drift_flag": 0,
        "misalignment_flag": 0,
        "checkpoint_event": None,
        "started_at": start_time,
        "ended_at": start_time + 1.0,
    })


def _seed_strong(store, n=5, domain="coding"):
    for _ in range(n):
        _seed_session(store, str(uuid.uuid4()), outcome="success", score=0.92,
                      drift=0.04, misalignment=0.04, domain=domain)


# ── run_learning_cycle ────────────────────────────────────────────────


def test_cycle_promotes_strong_strategy(tmp_path):
    store = _make_store(tmp_path)
    _seed_strong(store, n=5)

    summary = lcyc.run_learning_cycle(store._conn, since=0.0)

    assert summary["sessions_processed"] == 5
    assert summary["observations_persisted"] >= 5
    assert "strategy:coding" in summary["strategies"]
    decision = summary["decisions"]["strategy:coding"]
    assert decision["promotion_eligible"] is True
    assert decision["approved"] is True
    assert decision["event_type"] == lc.GOVERNANCE_EVENT_PROMOTE

    # The decision is a real append-only governance event with a valid chain.
    from agent.learning_governance import LearningGovernance
    assert LearningGovernance(store._conn).verify_chain("strategy:coding")


def test_cycle_no_decision_without_enough_samples(tmp_path):
    store = _make_store(tmp_path)
    _seed_strong(store, n=2)  # below EFFECTIVENESS_MIN_SAMPLE_COUNT (3)

    summary = lcyc.run_learning_cycle(store._conn, since=0.0)

    decision = summary["decisions"]["strategy:coding"]
    assert decision["promotion_eligible"] is False
    assert decision["approved"] is False


def test_cycle_skips_synthetic_and_low_evidence_sessions(tmp_path):
    store = _make_store(tmp_path)
    # synthetic -> excluded by get_eligible_sessions
    _seed_session(store, str(uuid.uuid4()), synthetic=1)
    # too few turns -> excluded
    _seed_session(store, str(uuid.uuid4()), turn_count=1)

    summary = lcyc.run_learning_cycle(store._conn, since=0.0)
    assert summary["sessions_processed"] == 0
    assert summary["strategies"] == []


def test_cycle_is_idempotent_on_observations(tmp_path):
    store = _make_store(tmp_path)
    _seed_strong(store, n=4)

    first = lcyc.run_learning_cycle(store._conn, since=0.0)
    second = lcyc.run_learning_cycle(store._conn, since=0.0)

    # Observations are deduped by (session, strategy, type, hash); the second
    # pass must not re-insert them.
    assert first["observations_persisted"] >= 4
    assert second["observations_persisted"] == 0


# ── apply_learned_authority ───────────────────────────────────────────


def test_apply_learned_authority_sets_safe_knobs(tmp_path):
    store = _make_store(tmp_path)
    _seed_strong(store, n=5)
    lcyc.run_learning_cycle(store._conn, since=0.0)

    agent = SimpleNamespace()
    applied = lcyc.apply_learned_authority(store._conn, agent, "coding")

    assert applied is True
    # Promotion directive is permissive; the safe knobs land on the agent.
    assert agent._authority_policy == lc.AUTHORITY_POLICY_PERMISSIVE
    assert getattr(agent, "_authority_context_size_override", None) is not None


def test_learned_authority_survives_per_turn_reset(tmp_path):
    """A learned (session-level) policy must persist across M1's per-turn reset.

    apply_learned_authority records the applied knobs as the per-turn baseline,
    so ensure_runtime_authority restores the learned policy each turn instead of
    reverting to hard permissive.
    """
    from agent.learning_governance import LearningGovernance
    from agent.runtime_authority import AuthorityDecision, ensure_runtime_authority

    store = _make_store(tmp_path)
    _seed_strong(store, n=5)
    # Record an ENFORCE directive that sets a non-default (strict) session policy.
    gov = LearningGovernance(store._conn)
    gov.enforce_policy(
        "strategy:coding",
        AuthorityDecision(policy=lc.AUTHORITY_POLICY_STRICT, allow_self_delegate=False),
        reason="enforce strict for test",
    )

    agent = SimpleNamespace()
    ensure_runtime_authority(agent)  # turn 1 start: no baseline yet -> permissive
    assert lcyc.apply_learned_authority(store._conn, agent, "coding") is True
    assert agent._authority_policy == lc.AUTHORITY_POLICY_STRICT
    assert agent._authority_delegation_policy == lc.AUTHORITY_POLICY_NO_SELF_DELEGATE

    # Turn 2 start: per-turn reset must restore the LEARNED baseline, not permissive.
    ensure_runtime_authority(agent)
    assert agent._authority_policy == lc.AUTHORITY_POLICY_STRICT
    assert agent._authority_delegation_policy == lc.AUTHORITY_POLICY_NO_SELF_DELEGATE
    assert agent._authority_allow_self_delegate is False


def test_digest_is_governance_idempotent(tmp_path):
    """Re-running the cycle on a stable strategy records no duplicate decision."""
    store = _make_store(tmp_path)
    _seed_strong(store, n=5)

    lcyc.run_learning_cycle(store._conn, since=0.0)
    n1 = store._conn.execute(
        "SELECT COUNT(*) FROM learning_governance_events WHERE event_type=?",
        (lc.GOVERNANCE_EVENT_PROMOTE,),
    ).fetchone()[0]
    lcyc.run_learning_cycle(store._conn, since=0.0)
    n2 = store._conn.execute(
        "SELECT COUNT(*) FROM learning_governance_events WHERE event_type=?",
        (lc.GOVERNANCE_EVENT_PROMOTE,),
    ).fetchone()[0]
    assert n1 == 1
    assert n2 == 1  # no duplicate PROMOTE on the second digest


def test_session_end_digest_is_throttled(tmp_path):
    store = _make_store(tmp_path)
    _seed_strong(store, n=5)
    marker = tmp_path / "last_digest"
    t0 = 1_000_000.0  # large, like a real time.time(), so the first run isn't throttled

    first = lcyc.maybe_run_session_end_digest(store._conn, now=t0, marker_path=str(marker))
    assert first is not None  # ran
    second = lcyc.maybe_run_session_end_digest(store._conn, now=t0 + 60, marker_path=str(marker))
    assert second is None  # throttled within the interval
    third = lcyc.maybe_run_session_end_digest(
        store._conn, now=t0 + lcyc._DIGEST_THROTTLE_SECONDS + 1, marker_path=str(marker)
    )
    assert third is not None  # interval elapsed -> ran again


def test_apply_returns_false_when_no_governance_event(tmp_path):
    store = _make_store(tmp_path)
    _seed_strong(store, n=2)  # not promotable -> no event recorded
    lcyc.run_learning_cycle(store._conn, since=0.0)

    agent = SimpleNamespace()
    assert lcyc.apply_learned_authority(store._conn, agent, "coding") is False
    assert not hasattr(agent, "_authority_policy")


def test_apply_never_sets_tool_scope_even_if_directive_has_one(tmp_path):
    """An ENFORCE directive carrying tool_scope must not filter the live tool list."""
    store = _make_store(tmp_path)
    _seed_strong(store, n=5)
    from agent.learning_governance import LearningGovernance
    from agent.runtime_authority import AuthorityDecision

    gov = LearningGovernance(store._conn)
    gov.enforce_policy(
        "strategy:coding",
        AuthorityDecision(policy=lc.AUTHORITY_POLICY_STRICT, tool_scope=("web_search",)),
        reason="manual enforce with tool scope",
    )

    agent = SimpleNamespace()
    applied = lcyc.apply_learned_authority(store._conn, agent, "coding")
    assert applied is True
    assert agent._authority_policy == lc.AUTHORITY_POLICY_STRICT
    # tool_scope is the cache-affecting knob and must be stripped at apply time.
    assert not hasattr(agent, "_authority_tool_scope")


def test_apply_refuses_tampered_chain(tmp_path):
    store = _make_store(tmp_path)
    _seed_strong(store, n=5)
    lcyc.run_learning_cycle(store._conn, since=0.0)

    # Tamper with the governance chain.
    store._conn.execute("UPDATE learning_governance_events SET source_hash='deadbeef'")
    store._conn.commit()

    agent = SimpleNamespace()
    assert lcyc.apply_learned_authority(store._conn, agent, "coding") is False


# ── strategy-id contract fix ──────────────────────────────────────────


def test_strategy_id_contract_holds_via_canonical_helper(tmp_path):
    """Building the evidence builder via strategy_id_for_domain reconciles ids.

    Observations are routed by primary_domain to strategy:{domain}. When the
    builder is constructed with that SAME id (what the orchestrator always
    does), StrategicLearning.record_case — which validates against
    evidence_builder.strategy_id — agrees, and derive_and_record does not raise
    the "Observation does not belong to this strategy" error the synthesis
    flagged. The footgun was hand-constructing a builder whose id diverged from
    the sessions fed to it; strategy_id_for_domain removes that possibility.
    """
    from agent.learning_evidence_builder import LearningEvidenceBuilder
    from agent.strategic_learning import StrategicDirective, StrategicLearning

    store = _make_store(tmp_path)
    session_id = str(uuid.uuid4())
    _seed_session(store, session_id, domain="research")

    sid = lcyc.strategy_id_for_domain("research")
    assert sid == "strategy:research"

    builder = LearningEvidenceBuilder(store._conn, sid)
    builder.build_and_persist(store.get_session(session_id), store.get_turns(session_id))
    obs = builder.get_observations(session_id)
    assert obs and obs[0]["strategy_id"] == sid

    sl = StrategicLearning(store._conn, builder)
    case_id = sl.record_case(
        obs[0]["observation_id"],
        StrategicDirective(
            strategy_id=sid,
            knob="max_tool_iterations",
            value=20,
            confidence=0.9,
            reason="contract test",
        ),
    )
    assert case_id


# ── verify_chain genesis-tamper fix ───────────────────────────────────


def test_verify_chain_detects_genesis_previous_hash_tamper(tmp_path):
    store = _make_store(tmp_path)
    _seed_strong(store, n=5)
    # Run the cycle so a real PROMOTE governance event (the genesis) exists.
    lcyc.run_learning_cycle(store._conn, since=0.0)
    from agent.learning_governance import LearningGovernance

    gov = LearningGovernance(store._conn)
    count = store._conn.execute(
        "SELECT COUNT(*) FROM learning_governance_events WHERE strategy_id='strategy:coding'"
    ).fetchone()[0]
    assert count >= 1
    assert gov.verify_chain("strategy:coding") is True

    # Tamper ONLY the genesis event's previous_hash (NULL -> a fake value).
    store._conn.execute(
        "UPDATE learning_governance_events SET previous_hash='forged' "
        "WHERE strategy_id='strategy:coding'"
    )
    store._conn.commit()
    assert gov.verify_chain("strategy:coding") is False
