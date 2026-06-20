"""Tests for circuit-breaker restore() chain-replay (was a skipped no-op)."""
import uuid

from agent import learning_constants as lc
from agent.learning_evidence_builder import LearningEvidenceBuilder
from agent.learning_governance import LearningGovernance
from agent.learning_mirror_circuit_breaker import LearningMirrorCircuitBreaker
from agent.opval.store import OpvalStore


def _seed_promote_event(store, strategy_id="strategy:coding"):
    """Create a real, valid governance chain (one PROMOTE event)."""
    for _ in range(5):
        sid = str(uuid.uuid4())
        store.insert_session({
            "session_id": sid, "task_id": "t", "parent_session_id": None,
            "root_session_id": sid, "platform": "test", "primary_domain": "coding",
            "secondary_domains": "[]", "start_time": 1.0, "end_time": 2.0, "turn_count": 1,
            "tool_execution_count": 1, "outcome": "success", "session_quality_score": 0.92,
            "session_tool_correctness": 0.92, "drift_pct": 0.04, "misalignment_pct": 0.04,
            "promotion_score": 0.92, "synthetic": 0, "synthetic_reason": None,
            "opval_enabled": 1, "recorded_at": 2.0,
        })
        LearningEvidenceBuilder(store._conn, strategy_id).build_and_persist(
            store.get_session(sid), [])
    gov = LearningGovernance(store._conn)
    decision = gov.promote_strategy(strategy_id)
    assert decision.approved
    return gov


def test_restore_succeeds_when_chain_intact(tmp_path):
    store = OpvalStore(str(tmp_path / "cbcr.db"))
    _seed_promote_event(store)
    cb = LearningMirrorCircuitBreaker(store._conn)
    cb.activate("drift alert")
    restored = cb.restore()
    assert restored is not None
    assert restored.state == lc.CIRCUIT_BREAKER_STATE_HEALTHY


def test_restore_refuses_when_chain_tampered(tmp_path):
    store = OpvalStore(str(tmp_path / "cbcr.db"))
    _seed_promote_event(store)
    cb = LearningMirrorCircuitBreaker(store._conn)
    cb.activate("drift alert")
    # Tamper the governance chain after activation.
    store._conn.execute("UPDATE learning_governance_events SET source_hash='deadbeef'")
    store._conn.commit()
    # Restore must refuse on a broken chain (safety).
    assert cb.restore() is None
    # And the breaker is still degraded (no healthy record written).
    assert cb.current_state().state == lc.CIRCUIT_BREAKER_STATE_DEGRADED
