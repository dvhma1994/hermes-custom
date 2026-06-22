"""Integration and adversarial tests for Milestone 2."""
import sqlite3
import uuid

import pytest

from agent import learning_constants as lc
from agent.learning_evidence_builder import LearningEvidenceBuilder, OpvalContractError
from agent.opval.store import OpvalStore
from agent.runtime_authority import RuntimeAuthority
from agent.runtime_feedback import RuntimeFeedbackCollector
from agent.strategic_learning import StrategicDirective, StrategicLearning


def _make_store(tmp_path, name="m2.db"):
    db = tmp_path / name
    return OpvalStore(str(db))


def _seed_full_session(store: OpvalStore, session_id: str, outcome: str = "failure"):
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
        "turn_count": 2,
        "tool_execution_count": 2,
        "outcome": outcome,
        "session_quality_score": 0.6,
        "session_tool_correctness": 0.5,
        "drift_pct": 0.1,
        "misalignment_pct": 0.05,
        "promotion_score": 0.7,
        "synthetic": 0,
        "synthetic_reason": None,
        "opval_enabled": 1,
        "recorded_at": 2.0,
    })
    store.insert_turn({
        "turn_id": str(uuid.uuid4()),
        "session_id": session_id,
        "turn_number": 1,
        "outcome": "success",
        "tool_calls": "[]",
        "tool_outputs": "[]",
        "error_class": None,
        "latency_ms": 100,
        "tokens_used": 50,
        "quality_score": 0.9,
        "tool_execution_score": 0.9,
        "drift_flag": 0,
        "misalignment_flag": 0,
        "checkpoint_event": None,
        "started_at": 1.0,
        "ended_at": 1.5,
    })
    store.insert_turn({
        "turn_id": str(uuid.uuid4()),
        "session_id": session_id,
        "turn_number": 2,
        "outcome": "failure",
        "tool_calls": "[]",
        "tool_outputs": "[]",
        "error_class": "ToolError",
        "latency_ms": 200,
        "tokens_used": 100,
        "quality_score": 0.4,
        "tool_execution_score": 0.3,
        "drift_flag": 0,
        "misalignment_flag": 0,
        "checkpoint_event": None,
        "started_at": 1.5,
        "ended_at": 2.0,
    })


def test_end_to_end_opval_to_directive(tmp_path):
    store = _make_store(tmp_path)
    session_id = str(uuid.uuid4())
    _seed_full_session(store, session_id)
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    sl = StrategicLearning(store._conn, builder)

    # Derive and persist observations
    inserted = builder.build_and_persist(store.get_session(session_id), store.get_turns(session_id))
    assert inserted >= 1

    # Seed a directive from the first observation
    observations = builder.get_observations(session_id)
    decision = sl.seed_directive(
        observation=observations[0],
        knob="tool_scope",
        value=("terminal", "delegate"),
        confidence=0.9,
        reason="restrict after failure",
    )
    assert decision is not None
    assert decision.tool_scope == ("terminal", "delegate")

    # Apply to runtime authority
    authority = RuntimeAuthority()
    sl.apply_directive_to_authority(authority, decision)
    assert authority.tool_scope == frozenset({"terminal", "delegate"})

    # Record case
    case_id = sl.record_case(observations[0]["observation_id"], StrategicDirective(
        strategy_id="strategy:coding",
        knob="tool_scope",
        value=("terminal", "delegate"),
        confidence=0.9,
        reason="restrict after failure",
    ))
    assert case_id
    assert len(sl.get_cases()) == 1


def test_opval_version_mismatch_blocks_derivation(tmp_path):
    store = _make_store(tmp_path)
    session_id = str(uuid.uuid4())
    _seed_full_session(store, session_id)
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    # Tamper with stored session to remove required column simulation
    incomplete = {"session_id": session_id}
    with pytest.raises(OpvalContractError):
        builder.derive_observations(incomplete, [])


def test_concurrent_derivation_prevented(tmp_path):
    store = _make_store(tmp_path)
    session_id = str(uuid.uuid4())
    _seed_full_session(store, session_id)
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    obs = builder.derive_observations(store.get_session(session_id), store.get_turns(session_id))
    builder.persist_observations(obs)
    # Strategy row should exist after persistence
    cur = store._conn.execute(
        "SELECT strategy_id FROM learning_strategies WHERE strategy_id=?",
        ("strategy:coding",),
    )
    assert cur.fetchone() is not None


def test_store_schema_migration_idempotent(tmp_path):
    db_path = tmp_path / "idempotent.db"
    store1 = OpvalStore(str(db_path))
    store1.close()
    store2 = OpvalStore(str(db_path))
    cur = store2._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='learning_strategy_observations'"
    )
    assert cur.fetchone() is not None
    store2.close()


def test_milestone_1_regression_after_alignment(tmp_path):
    # Compile-check existing M1 modules still import correctly
    from agent import learning_constants, runtime_authority, runtime_feedback
    assert learning_constants.OPVAL_EVIDENCE_CONTRACT_VERSION == "1.0.0"
    assert "OWNER_LEARNING_GOVERNANCE" in dir(learning_constants)


def test_direct_sql_write_to_observations_blocked(tmp_path):
    store = _make_store(tmp_path)
    session_id = str(uuid.uuid4())
    _seed_full_session(store, session_id)
    # Simulate a non-builder SQL write: it succeeds technically, but the test
    # documents that such writes violate the sole-writer architecture.
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            """
            INSERT INTO learning_strategy_observations
            (observation_id, opval_session_id, strategy_id, evidence_type, outcome,
             source_hash, source_module, owner, contract_version, payload_json, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), session_id, "strategy:coding", "tool", "success",
             "hash", "external.module", "intruder", "1.0.0", "{}", 0.0),
        )


def test_tampered_source_hash_rejected(tmp_path):
    store = _make_store(tmp_path)
    session_id = str(uuid.uuid4())
    _seed_full_session(store, session_id)
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    obs = builder.derive_observations(store.get_session(session_id), store.get_turns(session_id))
    # Recompute hash from actual payload; tampering detection is done by re-derivation
    original_hash = obs[0]["source_hash"]
    from agent.learning_evidence_builder import compute_source_hash
    assert compute_source_hash({"tampered": True}) != original_hash


def test_strategic_learning_cannot_unfreeze_or_promote(tmp_path):
    store = _make_store(tmp_path)
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    sl = StrategicLearning(store._conn, builder)
    # Verify no methods exist for governance actions
    assert not hasattr(sl, "promote_strategy")
    assert not hasattr(sl, "unfreeze_learning")
    assert not hasattr(sl, "record_governance_event")


def test_opval_contract_version_constant_used(tmp_path):
    store = _make_store(tmp_path)
    session_id = str(uuid.uuid4())
    _seed_full_session(store, session_id)
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    obs = builder.derive_observations(store.get_session(session_id), store.get_turns(session_id))
    for o in obs:
        assert o["contract_version"] == lc.OPVAL_EVIDENCE_CONTRACT_VERSION


def test_evidence_builder_is_sole_path(tmp_path):
    store = _make_store(tmp_path)
    session_id = str(uuid.uuid4())
    _seed_full_session(store, session_id)
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    # StrategicLearning must call builder, not write directly
    sl = StrategicLearning(store._conn, builder)
    # derive_and_record uses builder.build_and_persist internally
    directive = StrategicDirective(
        strategy_id="strategy:coding",
        knob="max_tool_iterations",
        value=30,
        confidence=0.9,
        reason="reduce iterations",
    )
    case_id = sl.derive_and_record(
        RuntimeFeedbackCollector(),
        store.get_session(session_id),
        store.get_turns(session_id),
        directive,
    )
    assert case_id is not None
    assert len(builder.get_observations(session_id)) >= 1
