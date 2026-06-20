"""Tests for LearningEvidenceBuilder._check_contract_version (was a no-op stub)."""
import uuid

import pytest

from agent import learning_constants as lc
from agent.learning_evidence_builder import LearningEvidenceBuilder, OpvalContractError
from agent.opval.store import OpvalStore


def _seed_obs(store, strategy_id="strategy:coding"):
    sid = str(uuid.uuid4())
    store.insert_session({
        "session_id": sid, "task_id": "t", "parent_session_id": None,
        "root_session_id": sid, "platform": "test", "primary_domain": "coding",
        "secondary_domains": "[]", "start_time": 1.0, "end_time": 2.0, "turn_count": 1,
        "tool_execution_count": 1, "outcome": "success", "session_quality_score": 0.9,
        "session_tool_correctness": 0.9, "drift_pct": 0.04, "misalignment_pct": 0.04,
        "promotion_score": 0.9, "synthetic": 0, "synthetic_reason": None,
        "opval_enabled": 1, "recorded_at": 2.0,
    })
    builder = LearningEvidenceBuilder(store._conn, strategy_id)
    builder.build_and_persist(store.get_session(sid), [])
    return builder


def test_contract_version_ok_for_current(tmp_path):
    store = OpvalStore(str(tmp_path / "cv.db"))
    builder = _seed_obs(store)
    # Persisted under the active contract version -> no drift, must not raise.
    builder._check_contract_version()


def test_contract_version_empty_store_ok(tmp_path):
    store = OpvalStore(str(tmp_path / "cv.db"))
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    # No evidence yet -> not a violation.
    builder._check_contract_version()


def test_contract_version_detects_drift(tmp_path):
    store = OpvalStore(str(tmp_path / "cv.db"))
    builder = _seed_obs(store)
    # Simulate evidence written under an older/incompatible contract version.
    store._conn.execute("UPDATE learning_strategy_observations SET contract_version='0.9.0'")
    store._conn.commit()
    with pytest.raises(OpvalContractError):
        builder._check_contract_version()


def test_build_and_persist_survives_drift_non_fatally(tmp_path):
    """Drift is a warning in the hot path, never fatal (so version upgrades work)."""
    store = OpvalStore(str(tmp_path / "cv.db"))
    builder = _seed_obs(store)
    store._conn.execute("UPDATE learning_strategy_observations SET contract_version='0.9.0'")
    store._conn.commit()
    # A second persist must not raise even though drift exists.
    sid = str(uuid.uuid4())
    store.insert_session({
        "session_id": sid, "task_id": "t", "parent_session_id": None,
        "root_session_id": sid, "platform": "test", "primary_domain": "coding",
        "secondary_domains": "[]", "start_time": 3.0, "end_time": 4.0, "turn_count": 1,
        "tool_execution_count": 1, "outcome": "success", "session_quality_score": 0.9,
        "session_tool_correctness": 0.9, "drift_pct": 0.04, "misalignment_pct": 0.04,
        "promotion_score": 0.9, "synthetic": 0, "synthetic_reason": None,
        "opval_enabled": 1, "recorded_at": 4.0,
    })
    builder.build_and_persist(store.get_session(sid), [])  # must not raise
