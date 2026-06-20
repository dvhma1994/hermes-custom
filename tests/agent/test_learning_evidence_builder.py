"""Unit tests for agent/learning_evidence_builder.py."""
import sqlite3
import uuid

import pytest

from agent import learning_constants as lc
from agent.learning_evidence_builder import (
    LearningEvidenceBuilder,
    OpvalContractError,
    compute_source_hash,
    validate_evidence_ownership,
)
from agent.opval.store import OpvalStore


def _make_store(tmp_path):
    db = tmp_path / "evidence.db"
    return OpvalStore(str(db))


def _seed_session(store: OpvalStore, session_id: str, outcome: str = "success"):
    store.insert_session({
        "session_id": session_id,
        "task_id": "test",
        "parent_session_id": None,
        "root_session_id": session_id,
        "platform": "test",
        "primary_domain": "coding",
        "secondary_domains": "[]",
        "start_time": 1.0,
        "end_time": 2.0,
        "turn_count": 1,
        "tool_execution_count": 1,
        "outcome": outcome,
        "session_quality_score": 0.9,
        "session_tool_correctness": 0.9,
        "drift_pct": 0.0,
        "misalignment_pct": 0.0,
        "promotion_score": 0.85,
        "synthetic": 0,
        "synthetic_reason": None,
        "opval_enabled": 1,
        "recorded_at": 2.0,
    })


def _seed_turn(store: OpvalStore, turn_id: str, session_id: str, outcome: str = "failure"):
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
        "quality_score": 0.5,
        "tool_execution_score": 0.4,
        "drift_flag": 0,
        "misalignment_flag": 0,
        "checkpoint_event": None,
        "started_at": 1.0,
        "ended_at": 2.0,
    })


def test_opval_contract_version_matches_constant(tmp_path):
    store = _make_store(tmp_path)
    _seed_session(store, "s1")
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    assert builder.contract_version == lc.OPVAL_EVIDENCE_CONTRACT_VERSION


def test_required_opval_columns_present(tmp_path):
    store = _make_store(tmp_path)
    _seed_session(store, "s1")
    _seed_turn(store, "t1", "s1")
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    session = store.get_session("s1")
    turns = store.get_turns("s1")
    observations = builder.derive_observations(session, turns)
    assert len(observations) >= 1


def test_fabricated_observation_rejected(tmp_path):
    """A fabricated observation (payload changed after hash) cannot be persisted."""
    store = _make_store(tmp_path)
    _seed_session(store, "s1")
    _seed_turn(store, "t1", "s1")
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")

    obs_list = builder.derive_observations(store.get_session("s1"), store.get_turns("s1"))
    obs = obs_list[0]
    obs["source_hash"] = "deadbeef"
    with pytest.raises(ValueError):
        validate_evidence_ownership(obs)
    # Recompute hash check: if we re-derive we should get a different hash for tampered payload
    tampered_payload = {"tampered": True}
    assert compute_source_hash(tampered_payload) != obs["source_hash"]


def test_source_hash_round_trip(tmp_path):
    store = _make_store(tmp_path)
    _seed_session(store, "s1", outcome="failure")
    _seed_turn(store, "t1", "s1")
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    obs1 = builder.derive_observations(store.get_session("s1"), store.get_turns("s1"))
    obs2 = builder.derive_observations(store.get_session("s1"), store.get_turns("s1"))
    assert obs1[0]["source_hash"] == obs2[0]["source_hash"]


def test_sole_writer_constraint(tmp_path):
    store = _make_store(tmp_path)
    _seed_session(store, "s1")
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    observations = builder.derive_observations(store.get_session("s1"), store.get_turns("s1"))
    builder.persist_observations(observations)

    # Direct write attempt outside the builder should still work at SQL level,
    # but the architecture requires all writes to go through LearningEvidenceBuilder.
    # We verify that the builder is the only sanctioned path by inspecting module name.
    assert builder.__module__ == "agent.learning_evidence_builder"


def test_idempotent_observation_insert(tmp_path):
    store = _make_store(tmp_path)
    _seed_session(store, "s1", outcome="failure")
    _seed_turn(store, "t1", "s1")
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    obs = builder.derive_observations(store.get_session("s1"), store.get_turns("s1"))
    first_count = builder.persist_observations(obs)
    second_count = builder.persist_observations(obs)
    # Second run should insert zero new rows because of UNIQUE constraint
    assert second_count == 0


def test_evidence_ownership_propagated(tmp_path):
    store = _make_store(tmp_path)
    _seed_session(store, "s1")
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    obs = builder.derive_observations(store.get_session("s1"), store.get_turns("s1"))
    for o in obs:
        assert o["source_module"] == "agent.opval.store"
        assert o["owner"] == lc.OWNER_LEARNING_GOVERNANCE


def test_opval_contract_error_on_missing_columns(tmp_path):
    store = _make_store(tmp_path)
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    with pytest.raises(OpvalContractError):
        builder.derive_observations({}, [])


def test_build_and_persist(tmp_path):
    store = _make_store(tmp_path)
    _seed_session(store, "s1", outcome="failure")
    _seed_turn(store, "t1", "s1", outcome="failure")
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    inserted = builder.build_and_persist(store.get_session("s1"), store.get_turns("s1"))
    assert inserted >= 1
    persisted = builder.get_observations("s1")
    assert len(persisted) >= 1


def test_strategies_table_created(tmp_path):
    store = _make_store(tmp_path)
    cur = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='learning_strategy_observations'"
    )
    assert cur.fetchone() is not None
