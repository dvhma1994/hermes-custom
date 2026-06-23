"""Integration and adversarial tests for Milestone 3b.

End-to-end flow: effectiveness → drift monitor → governance → circuit breaker →
strategic learning governance hook. Tests verify M3b constraints including
append-only governance chain, degraded-mode blocks, demotion-recommendation
triggers, and M3/M4 scope isolation.
"""
import time
import uuid

import pytest

from agent import learning_constants as lc
from agent.authority_alignment_monitor import AuthorityAlignmentMonitor
from agent.learning_dataset_manager import LearningDatasetManager
from agent.learning_demotion_recommendations import LearningDemotionRecommendations
from agent.learning_evidence_builder import LearningEvidenceBuilder
from agent.learning_governance import LearningGovernance
from agent.learning_mirror_circuit_breaker import LearningMirrorCircuitBreaker
from agent.opval.store import OpvalStore
from agent.runtime_authority import AuthorityDecision, RuntimeAuthority
from agent.strategic_learning import StrategicLearning
from agent.strategy_drift_monitor import StrategyDriftMonitor
from agent.strategy_effectiveness_manager import StrategyEffectivenessManager


def _make_store(tmp_path):
    return OpvalStore(str(tmp_path / "m3b.db"))


def _seed_session(store, session_id, outcome="success", score=0.85, drift=0.05, misalignment=0.05, domain="coding", synthetic=0):
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


def _seed_promotable_strategy(store, strategy_id="strategy:coding"):
    for _ in range(5):
        _obs_for_session(store, str(uuid.uuid4()), strategy_id=strategy_id, outcome="success", score=0.92, drift=0.04, misalignment=0.04)


def _seed_retirable_strategy(store, strategy_id="strategy:coding"):
    for _ in range(6):
        _obs_for_session(store, str(uuid.uuid4()), strategy_id=strategy_id, outcome="failure", score=0.25, drift=0.20, misalignment=0.20)


def _seed_drift_alert(store, strategy_id="strategy:coding"):
    # First batch: good performance to establish baseline
    for _ in range(5):
        _obs_for_session(store, str(uuid.uuid4()), strategy_id=strategy_id, outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    # Take a baseline snapshot
    from agent.strategy_drift_monitor import StrategyDriftMonitor
    StrategyDriftMonitor(store._conn).snapshot(strategy_id)
    # Second batch: much worse performance to create drift
    for _ in range(10):
        _obs_for_session(store, str(uuid.uuid4()), strategy_id=strategy_id, outcome="failure", score=0.2, drift=0.35, misalignment=0.35)


def test_end_to_end_promotion_requires_effectiveness_and_alignment(tmp_path):
    store = _make_store(tmp_path)
    _seed_promotable_strategy(store)
    eff = StrategyEffectivenessManager(store._conn)
    alignment = AuthorityAlignmentMonitor(store._conn, eff)
    breaker = LearningMirrorCircuitBreaker(store._conn)
    gov = LearningGovernance(store._conn, eff, alignment, breaker)

    decision = gov.promote_strategy("strategy:coding")
    assert decision.approved
    assert gov.verify_chain("strategy:coding")


def test_degraded_mode_blocks_promotion_and_allows_retirement(tmp_path):
    store = _make_store(tmp_path)
    _seed_promotable_strategy(store)
    _seed_retirable_strategy(store)

    gov = LearningGovernance(store._conn)
    gov._breaker.activate("drift alert")

    promote = gov.promote_strategy("strategy:coding")
    assert not promote.approved

    retire = gov.retire_strategy("strategy:coding")
    assert retire.approved


def test_drift_alert_triggers_circuit_breaker_and_freezes_promotion(tmp_path):
    store = _make_store(tmp_path)
    _seed_drift_alert(store)

    drift = StrategyDriftMonitor(store._conn)
    breaker = LearningMirrorCircuitBreaker(store._conn)
    eff = StrategyEffectivenessManager(store._conn)
    gov = LearningGovernance(store._conn, eff, AuthorityAlignmentMonitor(store._conn, eff), breaker)

    # Manually trigger circuit breaker on drift alert (expected M3b operational flow)
    if drift.is_drift_alert("strategy:coding"):
        breaker.activate("drift threshold exceeded")

    assert breaker.is_degraded()
    promote = gov.promote_strategy("strategy:coding")
    assert not promote.approved


def test_demotion_recommendation_flow(tmp_path):
    store = _make_store(tmp_path)
    # Create a few bad observations
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="failure", score=0.4, drift=0.10, misalignment=0.10)

    queue = LearningDemotionRecommendations(store._conn)
    queue.enqueue("DRIFT", "strategy:coding", "observed misalignment")

    gov = LearningGovernance(store._conn)
    decision = gov.retire_strategy("strategy:coding")
    assert decision.approved
    assert queue.get_unprocessed_count() == 0


def test_governance_event_application_to_authority(tmp_path):
    store = _make_store(tmp_path)
    _seed_promotable_strategy(store)
    gov = LearningGovernance(store._conn)
    decision = gov.promote_strategy("strategy:coding")
    assert decision.approved

    event = store._conn.execute("SELECT * FROM learning_governance_events WHERE event_type=?", (lc.GOVERNANCE_EVENT_PROMOTE,)).fetchone()
    event_id = event["event_id"]

    ra = RuntimeAuthority()
    sl = StrategicLearning(store._conn, LearningEvidenceBuilder(store._conn, "strategy:coding"))
    sl.apply_governance_event(ra, event_id)

    assert ra.get_policy() == lc.AUTHORITY_POLICY_PERMISSIVE


def test_dataset_batch_lineage_and_hash_integrity(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    dm = LearningDatasetManager(store._conn)
    t1 = time.time()
    b1 = dm.build_batch("strategy:coding", now=t1)
    dm.finalize_batch(b1.batch_id)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.95, drift=0.04, misalignment=0.04)
    b2 = dm.build_batch("strategy:coding", now=t1 + 1)
    assert b1.source_hash != b2.source_hash
    assert b1.batch_status == lc.DATASET_BATCH_STATUS_BUILDING
    assert dm.get_latest_ready_batch("strategy:coding").batch_id == b1.batch_id


def test_forced_refreeze_after_degraded_expiration(tmp_path):
    store = _make_store(tmp_path)
    breaker = LearningMirrorCircuitBreaker(store._conn)
    now = time.time()
    breaker.activate("drift alert", now=now)
    after = now + lc.CIRCUIT_BREAKER_DEGRADED_TIMEOUT_HOURS * 3600 + 5
    assert not breaker.is_degraded(now=after)
    assert breaker.current_state().state == lc.CIRCUIT_BREAKER_STATE_REFROZEN


def test_chain_tamper_detection_fails(tmp_path):
    store = _make_store(tmp_path)
    _seed_promotable_strategy(store)
    gov = LearningGovernance(store._conn)
    gov.promote_strategy("strategy:coding")
    # Tamper with source_hash
    store._conn.execute("UPDATE learning_governance_events SET source_hash = '000000'")
    store._conn.commit()
    assert not gov.verify_chain("strategy:coding")


def test_m3b_does_not_access_state_db(tmp_path):
    store = _make_store(tmp_path)
    _seed_promotable_strategy(store)
    gov = LearningGovernance(store._conn)
    gov.promote_strategy("strategy:coding")

    # No code in M3b references state.db in the project
    import os
    forbidden = False
    for path in [
        "agent/learning_governance.py",
        "agent/learning_mirror_circuit_breaker.py",
        "agent/strategy_drift_monitor.py",
        "agent/learning_dataset_manager.py",
        "agent/strategic_learning.py",
    ]:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if "state.db" in text:
            forbidden = True
    assert not forbidden


def test_no_milestone4_module_imports(tmp_path):
    # Verify M3b modules do not import M4-only concepts (active training, generation manager as generator, etc.)
    import ast
    for path in [
        "agent/learning_governance.py",
        "agent/learning_mirror_circuit_breaker.py",
        "agent/strategy_drift_monitor.py",
        "agent/learning_dataset_manager.py",
    ]:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
        names = set()
        for imp in imports:
            if isinstance(imp, ast.Import):
                names.update({alias.name for alias in imp.names})
            elif isinstance(imp, ast.ImportFrom) and imp.module:
                names.add(imp.module)
        assert "active_training" not in names
        assert "model_upload" not in names
        assert "fine_tuning" not in names
