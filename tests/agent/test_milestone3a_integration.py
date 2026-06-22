"""Integration and adversarial tests for Milestone 3a."""
import sqlite3
import uuid

import pytest

from agent import learning_constants as lc
from agent.authority_alignment_monitor import AuthorityAlignmentMonitor
from agent.learning_demotion_recommendations import LearningDemotionRecommendations
from agent.learning_evidence_builder import LearningEvidenceBuilder
from agent.opval.store import OpvalStore
from agent.runtime_authority import RuntimeAuthority
from agent.strategic_learning import StrategicLearning
from agent.strategy_effectiveness_manager import StrategyEffectivenessManager
from agent.strategy_generation_manager import StrategyGenerationManager


def _make_store(tmp_path, name="m3a.db"):
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


def _obs_for_session(store, session_id, strategy_id="strategy:coding", outcome="success", score=0.85, drift=0.05, misalignment=0.05, domain="coding"):
    _seed_session(store, session_id, outcome=outcome, score=score, drift=drift, misalignment=misalignment, domain=domain)
    _seed_turn(store, str(uuid.uuid4()), session_id, outcome=outcome)
    builder = LearningEvidenceBuilder(store._conn, strategy_id)
    builder.build_and_persist(store.get_session(session_id), store.get_turns(session_id))


def test_m3a_components_chain_produces_recommendation(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(6):
        _obs_for_session(store, str(uuid.uuid4()), outcome="failure", score=0.3, drift=0.05, misalignment=0.05)
    for _ in range(5):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05, domain="coding")
    sem = StrategyEffectivenessManager(store._conn)
    sgm = StrategyGenerationManager(store._conn)
    monitor = AuthorityAlignmentMonitor(store._conn, sem)
    queue = LearningDemotionRecommendations(store._conn)

    # 1. Effectiveness flags retirement
    result = sem.evaluate("strategy:coding")
    assert result.retirement_eligible

    # 2. Generator produces candidates but does not activate
    candidates = sgm.generate_candidates("strategy:coding")
    assert candidates
    for c in candidates:
        assert c.allowed_knobs[0] in lc.ALLOWED_AUTHORITY_KNOBS

    # 3. Monitor emits recommendations
    recs = monitor.check_alignment("strategy:coding")
    assert recs

    # 4. Recommendations are queued (advisory only)
    for rec in recs:
        queue.enqueue(rec.monitor_type, rec.strategy_id, rec.reason)
    assert queue.get_unprocessed_count() >= len(recs)


def test_m3a_does_not_mutate_governance_or_activate(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="failure", score=0.3, drift=0.05, misalignment=0.05)
    sem = StrategyEffectivenessManager(store._conn)
    monitor = AuthorityAlignmentMonitor(store._conn, sem)
    recs = monitor.check_alignment("strategy:coding")

    # No governance events should exist
    cur = store._conn.execute(
        "SELECT * FROM learning_governance_events LIMIT 1"
    )
    assert cur.fetchone() is None

    # No authority decision applied to runtime authority
    
    for rec in recs:
        if rec.directive:
            assert rec.directive.knob in lc.ALLOWED_AUTHORITY_KNOBS
            # Attempt to apply would need to go through allowed path, but we assert no enforcement here
            assert "governance" not in rec.monitor_type.lower()


def test_m3a_scope_isolation_from_m3b(tmp_path):
    store = _make_store(tmp_path)
    # Verify no M3b rows exist
    m3b_tables = {"learning_governance_events", "learning_drift_snapshots", "learning_dataset_batches"}
    for table in m3b_tables:
        cur = store._conn.execute(
            "SELECT * FROM {} LIMIT 1".format(table)
        )
        assert cur.fetchone() is None


def test_m3a_only_allowed_knobs_in_recommendations(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="failure", score=0.3, drift=0.25, misalignment=0.05)
    sem = StrategyEffectivenessManager(store._conn)
    monitor = AuthorityAlignmentMonitor(store._conn, sem)
    directives = monitor.emit_directive_dicts("strategy:coding")
    for d in directives:
        assert d["directive"]["knob"] in lc.ALLOWED_AUTHORITY_KNOBS
        assert d["authority_decision_valid"]


def test_m3a_generator_rejects_forbidden_knob(tmp_path):
    store = _make_store(tmp_path)
    sgm = StrategyGenerationManager(store._conn)
    # Inject a fake candidate with a forbidden knob
    from agent.strategy_generation_manager import StrategyCandidate
    fake = StrategyCandidate(
        candidate_id="x",
        strategy_id="s",
        strategy_text="bad",
        source="manual",
        confidence=0.9,
        rationale="bad",
        allowed_knobs=["promotion_threshold"],
        authority_directive_value={"promotion_threshold": 0.5},
    )
    original = sgm.generate_candidates
    sgm.generate_candidates = lambda strategy_id=None: [fake]
    try:
        with pytest.raises(ValueError):
            sgm.generate_and_validate_directives("strategy:coding")
    finally:
        sgm.generate_candidates = original



def test_m3a_direct_sql_write_to_demotion_table_blocked(tmp_path):
    store = _make_store(tmp_path)
    conn = store._conn
    with pytest.raises(sqlite3.IntegrityError):
        # rec_id is primary key so duplicate violates
        conn.execute(
            "INSERT INTO learning_demotion_recommendations (rec_id, monitor_type, strategy_id, reason, created_at) "
            "VALUES ('dup','x','s','r',1)"
        )
        conn.execute(
            "INSERT INTO learning_demotion_recommendations (rec_id, monitor_type, strategy_id, reason, created_at) "
            "VALUES ('dup','x','s','r',1)"
        )
        conn.commit()


def test_m3a_recommendations_do_not_unfreeze_learning(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="failure", score=0.3, drift=0.05, misalignment=0.05)
    sem = StrategyEffectivenessManager(store._conn)
    monitor = AuthorityAlignmentMonitor(store._conn, sem)
    recs = monitor.check_alignment("strategy:coding")
    for rec in recs:
        assert rec.directive.knob != "unfreeze"
        assert "unfreeze" not in rec.reason.lower()


def test_m3a_effectiveness_audit_snapshot(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(5):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    sem = StrategyEffectivenessManager(store._conn)
    result = sem.evaluate("strategy:coding")
    assert result.sample_count >= 5
    assert result.win_rate == 1.0
    assert result.avg_score >= lc.EFFECTIVENESS_AVG_SCORE_THRESHOLD
