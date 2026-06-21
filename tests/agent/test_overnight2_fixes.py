"""Regression tests for the overnight-2 hardening pass (verified bugs in the
now-active learning loop + agent core). Each test fails on the pre-fix code.
"""
import json

import agent.learning_constants as lc
from agent.strategy_effectiveness_manager import StrategyEffectivenessManager


def _sem():
    return StrategyEffectivenessManager.__new__(StrategyEffectivenessManager)


# ── Bug #1: turn-level 'recovery' observations were counted as samples, deflating
# win_rate and wrongly retiring strategies whose sessions all succeeded. ──────────
def test_recovery_observations_not_counted_as_samples():
    sem = _sem()

    def sess(outcome=lc.FEEDBACK_OUTCOME_SUCCESS):
        return {"evidence_type": "tool", "outcome": outcome,
                "payload_json": json.dumps({"session_quality_score": 1.0,
                                            "session_tool_correctness": 1.0})}

    def recovery():
        return {"evidence_type": "recovery", "outcome": lc.FEEDBACK_OUTCOME_FAILURE,
                "payload_json": "{}"}

    # 3 fully-successful sessions, each with 2 failing turns (-> 6 recovery obs)
    obs = [sess(), sess(), sess()] + [recovery()] * 6
    m = sem._extract_metrics(obs)
    assert m["sample_count"] == 3            # sessions, not 9
    assert m["win_rate"] == 1.0              # all sessions succeeded; not 0.33
    # therefore NOT retirement-eligible (0.33 would have been <= RETIREMENT_WIN_RATE)
    assert m["win_rate"] > lc.RETIREMENT_WIN_RATE


# ── Bug #2: governance hash-chain forked when two events shared created_at, so
# verify_chain() returned False on the engine's OWN untampered chain (and stuck
# the circuit breaker in DEGRADED). Ordering by rowid (insertion order) fixes it. ──
def test_governance_chain_stable_under_equal_timestamps():
    import agent.learning_governance as g
    import agent.learning_constants as lc
    from agent.opval.store import OpvalStore

    g.LearningGovernance._now = staticmethod(lambda: 1781990000.0)  # force identical created_at
    gov = g.LearningGovernance(OpvalStore(":memory:")._conn)
    ad = g.AuthorityDecision(policy=lc.AUTHORITY_POLICY_STRICT)
    for i in range(6):
        gov.enforce_policy("s", ad, f"r{i}")
    assert gov.verify_chain("s") is True            # untampered chain must verify
    # tamper detection must still work
    gov._conn.execute(
        "UPDATE learning_governance_events SET source_hash='deadbeef' "
        "WHERE strategy_id='s' AND rowid=(SELECT MIN(rowid)+1 FROM "
        "learning_governance_events WHERE strategy_id='s')"
    )
    assert gov.verify_chain("s") is False
