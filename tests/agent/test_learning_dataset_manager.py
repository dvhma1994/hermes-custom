"""Unit tests for agent/learning_dataset_manager.py."""
import time
import uuid

import pytest

from agent import learning_constants as lc
from agent.learning_dataset_manager import LearningDatasetManager
from agent.learning_evidence_builder import LearningEvidenceBuilder
from agent.opval.store import OpvalStore


def _make_store(tmp_path):
    return OpvalStore(str(tmp_path / "ds.db"))


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


def test_create_batch_from_cases_and_observations(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    dm = LearningDatasetManager(store._conn)
    batch = dm.build_batch("strategy:coding")
    assert batch.strategy_id == "strategy:coding"
    assert batch.observation_count == 3


def test_batch_version_monotonic(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    dm = LearningDatasetManager(store._conn)
    b1 = dm.build_batch("strategy:coding", now=time.time())
    b2 = dm.build_batch("strategy:coding", now=time.time() + 1)
    assert b2.created_at > b1.created_at


def test_synthetic_excluded_from_count(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(2):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05, synthetic=1)
    dm = LearningDatasetManager(store._conn)
    batch = dm.build_batch("strategy:coding")
    assert batch.observation_count == 2


def test_batch_ownership_learning_governance(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(2):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    dm = LearningDatasetManager(store._conn)
    batch = dm.build_batch("strategy:coding")
    assert batch.owner == lc.OWNER_LEARNING_GOVERNANCE


def test_finalize_and_deprecate_batch(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(2):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    dm = LearningDatasetManager(store._conn)
    batch = dm.build_batch("strategy:coding")
    assert batch.batch_status == lc.DATASET_BATCH_STATUS_BUILDING
    ready = dm.finalize_batch(batch.batch_id)
    assert ready is not None
    assert ready.batch_status == lc.DATASET_BATCH_STATUS_READY
    deprecated = dm.deprecate_batch(batch.batch_id)
    assert deprecated is not None
    assert deprecated.batch_status == lc.DATASET_BATCH_STATUS_DEPRECATED


def test_get_latest_ready_batch(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(2):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    dm = LearningDatasetManager(store._conn)
    batch = dm.build_batch("strategy:coding")
    dm.finalize_batch(batch.batch_id)
    latest = dm.get_latest_ready_batch("strategy:coding")
    assert latest is not None
    assert latest.batch_id == batch.batch_id


def test_empty_dataset_batch(tmp_path):
    store = _make_store(tmp_path)
    dm = LearningDatasetManager(store._conn)
    batch = dm.build_batch("strategy:coding")
    assert batch.observation_count == 0
    assert batch.case_count == 0


def test_source_hash_changes_with_data(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(2):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    dm = LearningDatasetManager(store._conn)
    b1 = dm.build_batch("strategy:coding", now=time.time())
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    b2 = dm.build_batch("strategy:coding", now=time.time() + 1)
    assert b1.source_hash != b2.source_hash
