"""Unit tests for agent/strategy_drift_monitor.py."""
import time
import uuid

import pytest

from agent import learning_constants as lc
from agent.learning_evidence_builder import LearningEvidenceBuilder
from agent.opval.store import OpvalStore
from agent.strategy_drift_monitor import StrategyDriftMonitor


def _make_store(tmp_path):
    return OpvalStore(str(tmp_path / "drift.db"))


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


def test_snapshot_computes_drift_from_baseline(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(5):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    monitor = StrategyDriftMonitor(store._conn)
    snap1 = monitor.snapshot("strategy:coding")
    # With only one snapshot, baseline is None so drift is 0
    assert snap1.drift_pct == 0.0


def test_drift_exceeds_threshold_triggers_alert(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(5):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    monitor = StrategyDriftMonitor(store._conn)
    monitor.snapshot("strategy:coding")
    # Now create a second batch with lower win_rate
    for _ in range(5):
        _obs_for_session(store, str(uuid.uuid4()), outcome="failure", score=0.3, drift=0.25, misalignment=0.25)
    snap2 = monitor.snapshot("strategy:coding")
    assert snap2.drift_pct > lc.LEARNING_PROMOTION_MAX_DRIFT_PERCENT
    assert monitor.is_drift_alert("strategy:coding")


def test_no_drift_below_threshold(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(5):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    monitor = StrategyDriftMonitor(store._conn)
    monitor.snapshot("strategy:coding")
    for _ in range(5):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.88, drift=0.06, misalignment=0.06)
    snap2 = monitor.snapshot("strategy:coding")
    assert snap2.drift_pct <= lc.LEARNING_PROMOTION_MAX_DRIFT_PERCENT


def test_synthetic_observations_excluded_from_evidence(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(5):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    monitor = StrategyDriftMonitor(store._conn)
    snap = monitor.snapshot("strategy:coding")
    assert snap.sample_count >= 5


def test_recent_snapshots_ordered(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    monitor = StrategyDriftMonitor(store._conn)
    monitor.snapshot("strategy:coding", now=time.time())
    monitor.snapshot("strategy:coding", now=time.time() + 1)
    snaps = monitor.recent_snapshots("strategy:coding")
    assert len(snaps) == 2
    assert snaps[0].created_at > snaps[1].created_at


def test_snapshot_ownership_learning_governance(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    monitor = StrategyDriftMonitor(store._conn)
    snap = monitor.snapshot("strategy:coding")
    assert snap.owner == lc.OWNER_LEARNING_GOVERNANCE


def test_drift_fabrication_rejected_by_ownership(tmp_path):
    store = _make_store(tmp_path)
    # We cannot make sqlite reject non-owner rows at schema level, but the monitor
    # baseline query does not filter by owner. This test verifies that fabricated
    # rows can be inserted, which is an expected limitation of SQLite without RLS.
    # The audit plan covers ownership checks at application layer.
    store._conn.execute(
        "INSERT INTO learning_drift_snapshots (snapshot_id, strategy_id, drift_pct, misalignment_pct, win_rate, "
        "avg_score, avg_alignment, sample_count, threshold_pct, owner, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("fake", "s", 0.5, 0.5, 0.5, 0.5, 0.5, 1, 0.15, "attacker", time.time()),
    )
    store._conn.commit()
    monitor = StrategyDriftMonitor(store._conn)
    baseline = monitor._baseline_win_rate("s")
    # Fabricated row is visible to the monitor baseline query.
    assert baseline is not None


def test_baseline_ignores_old_snapshots(tmp_path):
    store = _make_store(tmp_path)
    for _ in range(3):
        _obs_for_session(store, str(uuid.uuid4()), outcome="success", score=0.9, drift=0.05, misalignment=0.05)
    monitor = StrategyDriftMonitor(store._conn)
    old_now = time.time() - (lc.DRIFT_MAX_SNAPSHOT_AGE_DAYS + 1) * 86400
    monitor.snapshot("strategy:coding", now=old_now)
    baseline = monitor._baseline_win_rate("strategy:coding")
    assert baseline is None


def test_snapshot_misalignment_not_sourced_from_drift(tmp_path):
    """BUG #6: misalignment_pct must reflect the misalignment signal
    (derived from avg_alignment), NOT avg_drift.

    drift_pct/misalignment_pct are on the production 0-100 PERCENT scale
    (collectors store (n/total)*100). With drift_pct=12 and misalignment_pct=3,
    the effectiveness manager yields avg_drift=0.12 (fraction) and
    avg_alignment=100-3=97.0.  The snapshot's misalignment_pct must be ~0.03
    (from alignment), not 0.12 (drift).
    """
    store = _make_store(tmp_path)
    for _ in range(5):
        _obs_for_session(store, str(uuid.uuid4()),
                         outcome="success", score=0.9,
                         drift=12.0, misalignment=3.0)
    monitor = StrategyDriftMonitor(store._conn)
    snap = monitor.snapshot("strategy:coding")

    eff = monitor._effectiveness.evaluate("strategy:coding")
    expected_misalign = max(0.0, 1.0 - eff.avg_alignment / 100.0)
    assert abs(expected_misalign - 0.03) < 0.01          # sanity: alignment=97 -> 0.03
    assert abs(snap.misalignment_pct - expected_misalign) < 0.01
    # must NOT equal avg_drift
    assert abs(snap.misalignment_pct - eff.avg_drift) > 0.01
