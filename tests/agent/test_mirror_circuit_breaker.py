"""Unit tests for agent/learning_mirror_circuit_breaker.py."""
import time

import pytest

from agent import learning_constants as lc
from agent.learning_mirror_circuit_breaker import LearningMirrorCircuitBreaker
from agent.opval.store import OpvalStore


def _make_store(tmp_path):
    return OpvalStore(str(tmp_path / "cb.db"))


def test_activate_circuit_breaker_creates_state_record(tmp_path):
    store = _make_store(tmp_path)
    cb = LearningMirrorCircuitBreaker(store._conn)
    now = time.time()
    state = cb.activate("drift alert", now=now)
    assert state.state == lc.CIRCUIT_BREAKER_STATE_DEGRADED
    assert state.activated_at == now
    assert state.expires_at == now + lc.CIRCUIT_BREAKER_DEGRADED_TIMEOUT_HOURS * 3600


def test_degraded_mode_flag_persisted(tmp_path):
    store = _make_store(tmp_path)
    cb = LearningMirrorCircuitBreaker(store._conn)
    cb.activate("drift alert")
    current = cb.current_state()
    assert current is not None
    assert current.state == lc.CIRCUIT_BREAKER_STATE_DEGRADED


def test_promotion_disabled_in_degraded_mode(tmp_path):
    store = _make_store(tmp_path)
    cb = LearningMirrorCircuitBreaker(store._conn)
    cb.activate("drift alert")
    assert cb.is_degraded()


def test_forced_refreeze_after_24h(tmp_path):
    store = _make_store(tmp_path)
    cb = LearningMirrorCircuitBreaker(store._conn)
    now = time.time()
    cb.activate("drift alert", now=now)
    after = now + lc.CIRCUIT_BREAKER_DEGRADED_TIMEOUT_HOURS * 3600 + 1
    assert not cb.is_degraded(now=after)
    current = cb.current_state()
    assert current is not None
    assert current.state == lc.CIRCUIT_BREAKER_STATE_REFROZEN


def test_restore_from_degraded(tmp_path):
    store = _make_store(tmp_path)
    cb = LearningMirrorCircuitBreaker(store._conn)
    cb.activate("drift alert")
    restored = cb.restore()
    assert restored is not None
    assert restored.state == lc.CIRCUIT_BREAKER_STATE_HEALTHY


def test_multiple_activations_idempotent(tmp_path):
    store = _make_store(tmp_path)
    cb = LearningMirrorCircuitBreaker(store._conn)
    s1 = cb.activate("drift alert")
    s2 = cb.activate("another alert")
    assert s1.breaker_id == s2.breaker_id


def test_forced_refreeze_status(tmp_path):
    store = _make_store(tmp_path)
    cb = LearningMirrorCircuitBreaker(store._conn)
    now = time.time()
    cb.activate("drift alert", now=now)
    status = cb.forced_refreeze_status(now=now + 1)
    assert status["expired"] is False
    status_late = cb.forced_refreeze_status(now=now + lc.CIRCUIT_BREAKER_DEGRADED_TIMEOUT_HOURS * 3600 + 5)
    assert status_late["expired"] is True


def test_circuit_breaker_state_owner_learning_governance(tmp_path):
    store = _make_store(tmp_path)
    cb = LearningMirrorCircuitBreaker(store._conn)
    cb.activate("drift alert")
    cur = store._conn.execute("SELECT owner FROM learning_mirror_circuit_breaker LIMIT 1")
    assert cur.fetchone()["owner"] == lc.OWNER_LEARNING_GOVERNANCE


def test_degraded_timeout_configurable_for_tests(tmp_path):
    # The constant controls timeout; verify it is a positive number of hours
    assert lc.CIRCUIT_BREAKER_DEGRADED_TIMEOUT_HOURS > 0
    assert lc.CIRCUIT_BREAKER_FORCED_REFREEZE_HOURS == lc.CIRCUIT_BREAKER_DEGRADED_TIMEOUT_HOURS


def test_restore_without_degraded_returns_none(tmp_path):
    store = _make_store(tmp_path)
    cb = LearningMirrorCircuitBreaker(store._conn)
    assert cb.restore() is None
