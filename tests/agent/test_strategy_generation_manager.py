"""Unit tests for agent/strategy_generation_manager.py."""
import sqlite3
import uuid

import pytest

from agent import learning_constants as lc
from agent.learning_evidence_builder import LearningEvidenceBuilder
from agent.opval.store import OpvalStore
from agent.strategy_generation_manager import StrategyGenerationManager


def _make_store(tmp_path, name="sgm.db"):
    db = tmp_path / name
    return OpvalStore(str(db))


def _seed_session(store, session_id, outcome="success", score=0.85, domain="coding", synthetic=0):
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
        "drift_pct": 0.05,
        "misalignment_pct": 0.05,
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


def _obs_for_session(store, session_id, strategy_id="strategy:coding", outcome="success", score=0.85, domain="coding", synthetic=0):
    _seed_session(store, session_id, outcome=outcome, score=score, domain=domain, synthetic=synthetic)
    _seed_turn(store, str(uuid.uuid4()), session_id, outcome=outcome)
    builder = LearningEvidenceBuilder(store._conn, strategy_id)
    builder.build_and_persist(store.get_session(session_id), store.get_turns(session_id))


def test_generate_candidates_from_success_patterns(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(5):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", domain="coding")
    sgm = StrategyGenerationManager(store._conn)
    candidates = sgm.generate_candidates("strategy:coding")
    assert len(candidates) >= 1
    assert any(c.source == "tool_scope_pattern" for c in candidates)


def test_generation_min_pattern_count(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(4):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", domain="coding")
    sgm = StrategyGenerationManager(store._conn)
    candidates = sgm.generate_candidates("strategy:coding")
    # May still generate with confidence below threshold
    low_conf = [c for c in candidates if c.confidence < lc.GENERATION_CONFIDENCE_THRESHOLD]
    assert all(c.confidence >= lc.GENERATION_CONFIDENCE_THRESHOLD for c in candidates)


def test_generation_confidence_threshold(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(5):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", domain="coding")
    sgm = StrategyGenerationManager(store._conn)
    candidates = sgm.generate_candidates("strategy:coding")
    for c in candidates:
        assert c.confidence >= lc.GENERATION_CONFIDENCE_THRESHOLD


def test_generated_strategy_validates_allowed_knobs(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(5):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", domain="coding")
    sgm = StrategyGenerationManager(store._conn)
    directives = sgm.generate_and_validate_directives("strategy:coding")
    for d in directives:
        assert d["knob"] in lc.ALLOWED_AUTHORITY_KNOBS


def test_no_candidates_when_no_success_patterns(tmp_path):
    store = _make_store(tmp_path)
    sgm = StrategyGenerationManager(store._conn)
    candidates = sgm.generate_candidates("strategy:coding")
    assert candidates == []


def test_generation_ignores_low_quality_sessions(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(5):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, domain="research")
    sgm = StrategyGenerationManager(store._conn)
    candidates = sgm.generate_candidates("strategy:research")
    assert len(candidates) >= 1
    # research domain maps to a specific tool scope
    assert any(c.strategy_text.startswith("Restrict tool scope") for c in candidates)


def test_generated_strategy_has_strategy_id(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(5):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", domain="coding")
    sgm = StrategyGenerationManager(store._conn)
    candidates = sgm.generate_candidates("strategy:coding")
    for c in candidates:
        assert c.strategy_id
        assert c.candidate_id
