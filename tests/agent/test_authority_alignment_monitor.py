"""Unit tests for agent/authority_alignment_monitor.py."""
import sqlite3
import uuid

import pytest

from agent import learning_constants as lc
from agent.authority_alignment_monitor import AuthorityAlignmentMonitor
from agent.learning_evidence_builder import LearningEvidenceBuilder
from agent.opval.store import OpvalStore
from agent.strategic_learning import StrategicDirective
from agent.strategy_effectiveness_manager import StrategyEffectivenessManager


def _make_store(tmp_path, name="aam.db"):
    db = tmp_path / name
    return OpvalStore(str(db))


def _seed_session(store, session_id, outcome="success", score=0.85, drift=0.05, misalignment=0.05, domain="coding"):
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
        "synthetic": 0,
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


def _obs_for_session(store, session_id, strategy_id="strategy:coding", outcome="success", score=0.85, drift=0.05, misalignment=0.05):
    _seed_session(store, session_id, outcome=outcome, score=score, drift=drift, misalignment=misalignment)
    _seed_turn(store, str(uuid.uuid4()), session_id, outcome=outcome)
    builder = LearningEvidenceBuilder(store._conn, strategy_id)
    builder.build_and_persist(store.get_session(session_id), store.get_turns(session_id))


def test_alignment_monitor_emits_recommendation(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="failure", score=0.3, drift=0.05, misalignment=0.05)
    sem = StrategyEffectivenessManager(store._conn)
    monitor = AuthorityAlignmentMonitor(store._conn, sem)
    recs = monitor.check_alignment("strategy:coding")
    assert len(recs) >= 1
    assert any(r.directive.knob == "policy" for r in recs)


def test_alignment_monitor_rejects_forbidden_knob(tmp_path):
    store = _make_store(tmp_path)
    sem = StrategyEffectivenessManager(store._conn)
    monitor = AuthorityAlignmentMonitor(store._conn, sem)
    with pytest.raises(ValueError):
        monitor._build_safe_directive("strategy:coding", "promotion_threshold", 0.5, "bad")


def test_alignment_monitor_no_direct_authority_apply(tmp_path):
    store = _make_store(tmp_path)
    sem = StrategyEffectivenessManager(store._conn)
    monitor = AuthorityAlignmentMonitor(store._conn, sem)
    import inspect
    source = inspect.getsource(monitor.__class__)
    assert "apply_decision" not in source


def test_alignment_monitor_uses_effectiveness_data(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="failure", score=0.3, drift=0.05, misalignment=0.05)
    sem = StrategyEffectivenessManager(store._conn)
    monitor = AuthorityAlignmentMonitor(store._conn, sem)
    recs = monitor.check_alignment("strategy:coding")
    assert recs
    for r in recs:
        assert "0." in r.reason


def test_alignment_monitor_no_recommendation_when_aligned(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    sem = StrategyEffectivenessManager(store._conn)
    monitor = AuthorityAlignmentMonitor(store._conn, sem)
    recs = monitor.check_alignment("strategy:coding")
    assert recs == []


def test_alignment_monitor_recommendation_only_for_low_win_rate(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="failure", score=0.3, drift=0.05, misalignment=0.05)
    sem = StrategyEffectivenessManager(store._conn)
    monitor = AuthorityAlignmentMonitor(store._conn, sem)
    directives = monitor.emit_directive_dicts("strategy:coding")
    assert directives
    for d in directives:
        assert d["authority_decision_valid"]


def test_alignment_monitor_records_no_governance_event(tmp_path):
    store = _make_store(tmp_path)
    sem = StrategyEffectivenessManager(store._conn)
    monitor = AuthorityAlignmentMonitor(store._conn, sem)
    monitor.check_alignment("strategy:coding")
    cur = store._conn.execute(
        "SELECT * FROM learning_governance_events LIMIT 1"
    )
    assert cur.fetchone() is None


def test_alignment_monitor_respects_allowed_knobs(tmp_path):
    store = _make_store(tmp_path)
    sem = StrategyEffectivenessManager(store._conn)
    monitor = AuthorityAlignmentMonitor(store._conn, sem)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="failure", score=0.3, drift=0.25, misalignment=0.05)
    recs = monitor.check_alignment("strategy:coding")
    knobs = {r.directive.knob for r in recs}
    assert knobs.issubset(lc.ALLOWED_AUTHORITY_KNOBS)
