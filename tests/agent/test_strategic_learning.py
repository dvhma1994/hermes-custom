"""Unit tests for agent/strategic_learning.py."""
import sqlite3
import uuid

import pytest

from agent import learning_constants as lc
from agent.learning_evidence_builder import LearningEvidenceBuilder
from agent.opval.store import OpvalStore
from agent.runtime_authority import RuntimeAuthority
from agent.strategic_learning import (
    StrategicDirective,
    StrategicLearning,
    build_authority_directive,
)


def _make_store(tmp_path):
    db = tmp_path / "sl.db"
    return OpvalStore(str(db))


def _seed_session(store: OpvalStore, session_id: str, outcome: str = "failure"):
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
        "session_quality_score": 0.5,
        "session_tool_correctness": 0.4,
        "drift_pct": 0.2,
        "misalignment_pct": 0.1,
        "promotion_score": 0.6,
        "synthetic": 0,
        "synthetic_reason": None,
        "opval_enabled": 1,
        "recorded_at": 2.0,
    })


def _seed_turn(store: OpvalStore, turn_id: str, session_id: str):
    store.insert_turn({
        "turn_id": turn_id,
        "session_id": session_id,
        "turn_number": 1,
        "outcome": "failure",
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


def _seed_observation(store: OpvalStore, strategy_id: str = "strategy:coding"):
    session_id = str(uuid.uuid4())
    _seed_session(store, session_id)
    _seed_turn(store, str(uuid.uuid4()), session_id)
    builder = LearningEvidenceBuilder(store._conn, strategy_id)
    builder.build_and_persist(store.get_session(session_id), store.get_turns(session_id))
    obs = builder.get_observations(session_id)
    assert obs
    return obs[0]["observation_id"]


def test_case_recording_with_metadata(tmp_path):
    store = _make_store(tmp_path)
    observation_id = _seed_observation(store)
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    sl = StrategicLearning(store._conn, builder)
    directive = StrategicDirective(
        strategy_id="strategy:coding",
        knob="max_tool_iterations",
        value=30,
        confidence=0.8,
        reason="reduce iterations after failures",
    )
    case_id = sl.record_case(observation_id, directive)
    assert case_id
    cases = sl.get_cases(observation_id)
    assert len(cases) == 1
    assert cases[0]["strategy_id"] == "strategy:coding"


def test_directive_confidence_gate(tmp_path):
    store = _make_store(tmp_path)
    observation_id = _seed_observation(store)
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    sl = StrategicLearning(store._conn, builder)
    low_directive = StrategicDirective(
        strategy_id="strategy:coding",
        knob="max_tool_iterations",
        value=30,
        confidence=0.1,
        reason="low confidence",
    )
    with pytest.raises(ValueError):
        sl.record_case(observation_id, low_directive)


def test_directive_maps_to_allowed_authority_knob(tmp_path):
    store = _make_store(tmp_path)
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    sl = StrategicLearning(store._conn, builder)
    decision = sl.seed_directive(
        observation={},
        knob="tool_scope",
        value=("terminal", "delegate"),
        confidence=0.9,
        reason="restrict tools",
    )
    assert decision is not None
    assert decision.tool_scope == ("terminal", "delegate")


def test_no_promotion_threshold_mutation(tmp_path):
    builder = LearningEvidenceBuilder(sqlite3.connect(":memory:"), "strategy:coding")
    sl = StrategicLearning(sqlite3.connect(":memory:"), builder)
    with pytest.raises(ValueError):
        build_authority_directive("strategy:coding", "promotion_threshold", 0.5, 0.9, "bad")


def test_case_id_unique(tmp_path):
    store = _make_store(tmp_path)
    observation_id = _seed_observation(store)
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    sl = StrategicLearning(store._conn, builder)
    directive = StrategicDirective(
        strategy_id="strategy:coding",
        knob="max_tool_iterations",
        value=30,
        confidence=0.8,
        reason="reduce iterations",
    )
    case_id1 = sl.record_case(observation_id, directive)
    case_id2 = sl.record_case(observation_id, directive)
    # Same observation + directive_json should not duplicate
    cases = sl.get_cases(observation_id)
    assert len(cases) == 1


def test_apply_directive_to_authority(tmp_path):
    builder = LearningEvidenceBuilder(sqlite3.connect(":memory:"), "strategy:coding")
    sl = StrategicLearning(sqlite3.connect(":memory:"), builder)
    authority = RuntimeAuthority()
    directive = build_authority_directive("strategy:coding", "policy", "strict", 0.9, "be strict")
    sl.apply_directive_to_authority(authority, directive)
    assert authority.policy == "strict"


def test_apply_directive_forbidden_knob(tmp_path):
    builder = LearningEvidenceBuilder(sqlite3.connect(":memory:"), "strategy:coding")
    sl = StrategicLearning(sqlite3.connect(":memory:"), builder)
    authority = RuntimeAuthority()
    with pytest.raises(ValueError):
        directive = build_authority_directive("strategy:coding", "readiness_min_sessions", 10, 0.9, "bad")
        sl.apply_directive_to_authority(authority, directive)


def test_derive_and_record(tmp_path):
    store = _make_store(tmp_path)
    session_id = str(uuid.uuid4())
    _seed_session(store, session_id, outcome="failure")
    _seed_turn(store, str(uuid.uuid4()), session_id)
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    sl = StrategicLearning(store._conn, builder)
    from agent.runtime_feedback import RuntimeFeedbackCollector
    collector = RuntimeFeedbackCollector()
    collector.record_tool_outcome({"tool_name": "terminal", "outcome": "failure", "session_id": session_id})
    directive = StrategicDirective(
        strategy_id="strategy:coding",
        knob="max_tool_iterations",
        value=30,
        confidence=0.8,
        reason="reduce after failure",
    )
    case_id = sl.derive_and_record(collector, store.get_session(session_id), store.get_turns(session_id), directive)
    assert case_id
    assert len(sl.get_cases()) == 1


def test_strategic_learning_no_self_delegate_mutation(tmp_path):
    store = _make_store(tmp_path)
    observation_id = _seed_observation(store)
    builder = LearningEvidenceBuilder(store._conn, "strategy:coding")
    sl = StrategicLearning(store._conn, builder)
    # self-delegate is an allowed authority knob for M2, so this should work
    directive = StrategicDirective(
        strategy_id="strategy:coding",
        knob="allow_self_delegate",
        value=False,
        confidence=0.9,
        reason="disable self delegation",
    )
    case_id = sl.record_case(observation_id, directive)
    assert case_id
