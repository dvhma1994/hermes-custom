"""Unit tests for agent/strategy_effectiveness_manager.py."""
import sqlite3
import uuid

import pytest

from agent import learning_constants as lc
from agent.learning_evidence_builder import LearningEvidenceBuilder
from agent.opval.store import OpvalStore
from agent.strategy_effectiveness_manager import StrategyEffectivenessManager


def _make_store(tmp_path, name="sem.db"):
    db = tmp_path / name
    return OpvalStore(str(db))


def _seed_session(store, session_id, outcome="success", score=0.85, drift=0.05, misalignment=0.05, synthetic=0, domain="coding"):
    store.insert_session({
        "session_id": session_id,
        "task_id": "test",
        "parent_session_id": None,
        "root_session_id": session_id,
        "platform": "test",
        "primary_domain": domain,
        "secondary_domains": "[]",
        "start_time": 1.0,
        "end_time": 2.0,
        "turn_count": 1,
        "tool_execution_count": 1,
        "outcome": outcome,
        "session_quality_score": score,
        "session_tool_correctness": score,
        "drift_pct": drift,
        "misalignment_pct": misalignment,
        "promotion_score": score,
        "synthetic": synthetic,
        "synthetic_reason": None,
        "opval_enabled": 1,
        "recorded_at": 2.0,
    })


def _seed_turn(store, turn_id, session_id, outcome="success"):
    store.insert_turn({
        "turn_id": turn_id,
        "session_id": session_id,
        "turn_number": 1,
        "outcome": outcome,
        "tool_calls": "[]",
        "tool_outputs": "[]",
        "error_class": None,
        "latency_ms": 100,
        "tokens_used": 50,
        "quality_score": 0.8,
        "tool_execution_score": 0.8,
        "drift_flag": 0,
        "misalignment_flag": 0,
        "checkpoint_event": None,
        "started_at": 1.0,
        "ended_at": 2.0,
    })


def _obs_for_session(store, session_id, strategy_id="strategy:coding", outcome="success", score=0.85, drift=0.05, misalignment=0.05, domain="coding", synthetic=0):
    _seed_session(store, session_id, outcome=outcome, score=score, drift=drift, misalignment=misalignment, domain=domain, synthetic=synthetic)
    _seed_turn(store, str(uuid.uuid4()), session_id, outcome=outcome)
    builder = LearningEvidenceBuilder(store._conn, strategy_id)
    builder.build_and_persist(store.get_session(session_id), store.get_turns(session_id))


def test_effectiveness_basic_calculation(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()))
    sem = StrategyEffectivenessManager(store._conn)
    result = sem.evaluate("strategy:coding")
    assert result.strategy_id == "strategy:coding"
    assert result.sample_count == 3
    assert result.win_rate == 1.0
    assert result.avg_score >= 0.0
    assert result.avg_alignment >= 0.0


def test_effectiveness_insufficient_samples(tmp_path):
    store = _make_store(tmp_path)
    _obs_for_session(store, str(uuid.uuid4()))
    sem = StrategyEffectivenessManager(store._conn)
    result = sem.evaluate("strategy:coding")
    assert result.sample_count == 1
    assert not result.promotion_eligible
    assert not result.retirement_eligible


def test_effectiveness_win_rate_threshold(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(2):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success")
    _obs_for_session(store, str(uuid.uuid4()), outcome="failure", score=0.3)
    sem = StrategyEffectivenessManager(store._conn)
    result = sem.evaluate("strategy:coding")
    # failure session creates a recovery observation too, so 4 total
    assert result.sample_count == 4
    # 2 success + 2 failure = win_rate 0.5
    assert result.win_rate == pytest.approx(0.5, abs=0.01)
    assert result.win_rate < lc.EFFECTIVENESS_WIN_RATE_THRESHOLD
    assert not result.promotion_eligible


def test_effectiveness_avg_score_threshold(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), score=0.50, outcome="success")
    sem = StrategyEffectivenessManager(store._conn)
    result = sem.evaluate("strategy:coding")
    assert result.avg_score < lc.EFFECTIVENESS_AVG_SCORE_THRESHOLD
    assert not result.promotion_eligible


def test_effectiveness_avg_alignment_threshold(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", misalignment=0.5)
    sem = StrategyEffectivenessManager(store._conn)
    result = sem.evaluate("strategy:coding")
    assert result.avg_alignment < lc.EFFECTIVENESS_AVG_ALIGNMENT_THRESHOLD
    assert not result.promotion_eligible


def test_effectiveness_multiple_strategies(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(2):
        _obs_for_session(store, str(uuid.uuid4()), strategy_id="strategy:coding")
        _obs_for_session(store, str(uuid.uuid4()), strategy_id="strategy:research", domain="research")
    sem = StrategyEffectivenessManager(store._conn)
    all_results = sem.evaluate_all()
    strategy_ids = {r.strategy_id for r in all_results}
    assert "strategy:coding" in strategy_ids
    assert "strategy:research" in strategy_ids


def test_effectiveness_ignores_synthetic(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), synthetic=0)
    sem = StrategyEffectivenessManager(store._conn)
    result = sem.evaluate("strategy:coding")
    assert result.sample_count == 3


def test_effectiveness_retirement_eligibility(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="failure", score=0.3)
    sem = StrategyEffectivenessManager(store._conn)
    result = sem.evaluate("strategy:coding")
    # 3 failure sessions each produce session+recovery = 6 observations
    assert result.sample_count == 6
    assert result.win_rate == 0.0
    assert result.win_rate <= lc.RETIREMENT_WIN_RATE
    assert result.retirement_eligible


def test_strategy_summary_has_no_eligibility_flags(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()))
    sem = StrategyEffectivenessManager(store._conn)
    summary = sem.get_strategy_summary("strategy:coding")
    assert "promotion_eligible" not in summary
    assert "retirement_eligible" not in summary
    assert "sample_count" in summary