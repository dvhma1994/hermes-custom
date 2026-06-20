"""
Targeted regression tests for Category A remediation.
A2: Governance bypass — apply_governance_event must reject fabricated events.
A3: Audit chain integrity — verify_chain must detect deletion of intermediate events.
"""
import os, sys, time, uuid, json, sqlite3
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from agent.opval.store import OpvalStore
from agent.runtime_authority import RuntimeAuthority
from agent.strategic_learning import StrategicLearning
from agent.learning_governance import LearningGovernance
from agent.learning_evidence_builder import LearningEvidenceBuilder
from agent.learning_mirror_circuit_breaker import LearningMirrorCircuitBreaker
from agent import learning_constants as lc


def _seed_promotable(store, domain='coding', count=60):
    for _ in range(count):
        sid = str(uuid.uuid4())
        store.insert_session({
            'session_id': sid, 'task_id': 't', 'parent_session_id': None,
            'root_session_id': sid, 'platform': 'test', 'primary_domain': domain,
            'secondary_domains': '[]', 'start_time': time.time(), 'end_time': time.time()+1,
            'turn_count': 1, 'tool_execution_count': 1, 'outcome': 'success',
            'session_quality_score': 95.0, 'session_tool_correctness': 95.0,
            'drift_pct': 0.02, 'misalignment_pct': 0.02, 'promotion_score': 95.0,
            'synthetic': 0, 'synthetic_reason': None, 'opval_enabled': 1, 'recorded_at': time.time(),
        })
        store.insert_turn({
            'turn_id': str(uuid.uuid4()), 'session_id': sid, 'turn_number': 1, 'outcome': 'success',
            'tool_calls': '[]', 'tool_outputs': '[]', 'error_class': None, 'latency_ms': 100,
            'tokens_used': 50, 'quality_score': 95.0, 'tool_execution_score': 95.0,
            'drift_flag': 0, 'misalignment_flag': 0, 'checkpoint_event': None,
            'started_at': time.time(), 'ended_at': time.time()+1,
        })
        builder = LearningEvidenceBuilder(store._conn, f'strategy:{domain}')
        builder.build_and_persist(store.get_session(sid), store.get_turns(sid))


def _seed_retirable(store, domain='coding', count=60):
    for _ in range(count):
        sid = str(uuid.uuid4())
        store.insert_session({
            'session_id': sid, 'task_id': 't', 'parent_session_id': None,
            'root_session_id': sid, 'platform': 'test', 'primary_domain': domain,
            'secondary_domains': '[]', 'start_time': time.time(), 'end_time': time.time()+1,
            'turn_count': 1, 'tool_execution_count': 1, 'outcome': 'failure',
            'session_quality_score': 20.0, 'session_tool_correctness': 20.0,
            'drift_pct': 0.30, 'misalignment_pct': 0.25, 'promotion_score': 20.0,
            'synthetic': 0, 'synthetic_reason': None, 'opval_enabled': 1, 'recorded_at': time.time(),
        })
        store.insert_turn({
            'turn_id': str(uuid.uuid4()), 'session_id': sid, 'turn_number': 1, 'outcome': 'failure',
            'tool_calls': '[]', 'tool_outputs': '[]', 'error_class': None, 'latency_ms': 100,
            'tokens_used': 50, 'quality_score': 20.0, 'tool_execution_score': 20.0,
            'drift_flag': 1, 'misalignment_flag': 1, 'checkpoint_event': None,
            'started_at': time.time(), 'ended_at': time.time()+1,
        })
        builder = LearningEvidenceBuilder(store._conn, f'strategy:{domain}')
        builder.build_and_persist(store.get_session(sid), store.get_turns(sid))


# ============================================================
# A2: Governance Bypass Regression Tests
# ============================================================

class TestA2GovernanceBypass:

    def test_fabricated_dict_raises(self, tmp_path):
        """Fabricated event dict must be rejected — only event_id is accepted."""
        store = OpvalStore(str(tmp_path / "test.db"))
        _seed_promotable(store)
        sl = StrategicLearning(store._conn, LearningEvidenceBuilder(store._conn, "strategy:coding"))
        ra = RuntimeAuthority()
        fake_event = {"status": "APPLIED", "authority_directive_json": '{"policy": "permissive"}', "source_hash": "fake"}
        with pytest.raises((sqlite3.ProgrammingError, TypeError)):
            sl.apply_governance_event(ra, fake_event)

    def test_fabricated_event_id_raises(self, tmp_path):
        """Fabricated event_id must be rejected."""
        store = OpvalStore(str(tmp_path / "test.db"))
        _seed_promotable(store)
        sl = StrategicLearning(store._conn, LearningEvidenceBuilder(store._conn, "strategy:coding"))
        ra = RuntimeAuthority()
        with pytest.raises(ValueError, match="not found"):
            sl.apply_governance_event(ra, "nonexistent-event-id")

    def test_rejected_event_raises(self, tmp_path):
        """Event with status REJECTED must raise ValueError."""
        store = OpvalStore(str(tmp_path / "test.db"))
        gov = LearningGovernance(store._conn)
        # Insert a REJECTED event manually
        store._conn.execute(
            "INSERT INTO learning_governance_events (event_id, event_type, strategy_id, decision_reason, "
            "previous_hash, source_hash, authority_directive_json, status, owner, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("test-rej", "PROMOTE", "strategy:coding", "test", None,
             "fakehash", '{"policy": "permissive"}', lc.GOVERNANCE_STATUS_REJECTED,
             lc.OWNER_LEARNING_GOVERNANCE, time.time())
        )
        sl = StrategicLearning(store._conn, LearningEvidenceBuilder(store._conn, "strategy:coding"))
        ra = RuntimeAuthority()
        with pytest.raises(ValueError, match="Rejected"):
            sl.apply_governance_event(ra, "test-rej")

    def test_tampered_source_hash_raises(self, tmp_path):
        """Event with tampered source_hash must be detected."""
        store = OpvalStore(str(tmp_path / "test.db"))
        _seed_promotable(store)
        gov = LearningGovernance(store._conn)
        gov.promote_strategy("strategy:coding")

        event = store._conn.execute(
            "SELECT event_id FROM learning_governance_events WHERE event_type=?",
            (lc.GOVERNANCE_EVENT_PROMOTE,)
        ).fetchone()

        # Tamper the source_hash
        store._conn.execute(
            "UPDATE learning_governance_events SET source_hash=? WHERE event_id=?",
            ("tampered_hash", event["event_id"])
        )

        sl = StrategicLearning(store._conn, LearningEvidenceBuilder(store._conn, "strategy:coding"))
        ra = RuntimeAuthority()
        with pytest.raises(ValueError, match="source_hash validation failed"):
            sl.apply_governance_event(ra, event["event_id"])

    def test_valid_applied_event_succeeds(self, tmp_path):
        """Valid APPLIED governance event must apply correctly."""
        store = OpvalStore(str(tmp_path / "test.db"))
        _seed_promotable(store)
        gov = LearningGovernance(store._conn)
        gov.promote_strategy("strategy:coding")

        event = store._conn.execute(
            "SELECT event_id, status FROM learning_governance_events WHERE event_type=?",
            (lc.GOVERNANCE_EVENT_PROMOTE,)
        ).fetchone()

        sl = StrategicLearning(store._conn, LearningEvidenceBuilder(store._conn, "strategy:coding"))
        ra = RuntimeAuthority()
        sl.apply_governance_event(ra, event["event_id"])
        assert ra.get_policy() == lc.AUTHORITY_POLICY_PERMISSIVE

    def test_governance_bypass_not_reproducible(self, tmp_path):
        """Verify the original A2 exploit is no longer possible."""
        store = OpvalStore(str(tmp_path / "test.db"))
        _seed_promotable(store)
        sl = StrategicLearning(store._conn, LearningEvidenceBuilder(store._conn, "strategy:coding"))
        ra = RuntimeAuthority()

        # The old exploit: pass a fabricated dict with no DB record
        old_exploit = {"status": "APPLIED", "authority_directive_json": '{"policy": "permissive", "max_tool_iterations": 999}', "source_hash": "bypass"}
        with pytest.raises(Exception):
            sl.apply_governance_event(ra, old_exploit)

        # Verify no authority change occurred from the bypass attempt
        assert ra.get_max_tool_iterations() != 999


# ============================================================
# A3: Audit Chain Integrity Regression Tests
# ============================================================

class TestA3AuditChainIntegrity:

    def test_valid_chain_returns_true(self, tmp_path):
        """Valid unbroken chain must return True."""
        store = OpvalStore(str(tmp_path / "test.db"))
        _seed_promotable(store)
        gov = LearningGovernance(store._conn)
        gov.promote_strategy("strategy:coding")
        assert gov.verify_chain("strategy:coding") is True

    def test_single_event_chain_returns_true(self, tmp_path):
        """Single event chain must return True (previous_hash is None)."""
        store = OpvalStore(str(tmp_path / "test.db"))
        _seed_promotable(store)
        gov = LearningGovernance(store._conn)
        gov.promote_strategy("strategy:coding")
        events = gov.replay_events("strategy:coding")
        assert len(events) == 1
        assert gov.verify_chain("strategy:coding") is True

    def test_middle_event_deleted_returns_false(self, tmp_path):
        """Deleting an intermediate event must return False."""
        store = OpvalStore(str(tmp_path / "test.db"))
        _seed_promotable(store)
        gov = LearningGovernance(store._conn)
        gov.promote_strategy("strategy:coding")
        _seed_retirable(store)
        gov.retire_strategy("strategy:coding")

        assert gov.verify_chain("strategy:coding") is True

        # Delete the PROMOTE event (first event by created_at)
        store._conn.execute(
            "DELETE FROM learning_governance_events WHERE event_type=?",
            (lc.GOVERNANCE_EVENT_PROMOTE,)
        )

        # Chain must now be invalid: RETIRE's previous_hash points to deleted event's source_hash
        assert gov.verify_chain("strategy:coding") is False

    def test_previous_hash_tampered_returns_false(self, tmp_path):
        """Tampering with previous_hash must return False."""
        store = OpvalStore(str(tmp_path / "test.db"))
        _seed_promotable(store)
        gov = LearningGovernance(store._conn)
        gov.promote_strategy("strategy:coding")
        _seed_retirable(store)
        gov.retire_strategy("strategy:coding")

        # Tamper the second event's previous_hash
        store._conn.execute(
            "UPDATE learning_governance_events SET previous_hash=? WHERE event_type=?",
            ("tampered", lc.GOVERNANCE_EVENT_RETIRE)
        )

        assert gov.verify_chain("strategy:coding") is False

    def test_source_hash_tampered_returns_false(self, tmp_path):
        """Tampering with source_hash must return False."""
        store = OpvalStore(str(tmp_path / "test.db"))
        _seed_promotable(store)
        gov = LearningGovernance(store._conn)
        gov.promote_strategy("strategy:coding")

        store._conn.execute(
            "UPDATE learning_governance_events SET source_hash=? WHERE event_type=?",
            ("tampered", lc.GOVERNANCE_EVENT_PROMOTE)
        )

        assert gov.verify_chain("strategy:coding") is False

    def test_empty_chain_returns_true(self, tmp_path):
        """Empty chain (no events) must return True."""
        store = OpvalStore(str(tmp_path / "test.db"))
        gov = LearningGovernance(store._conn)
        assert gov.verify_chain("strategy:nonexistent") is True
