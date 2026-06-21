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
