"""Learning mirror circuit breaker for Learning Governance V1.

Provides a safety shutdown for the learning governance system. When activated,
the breaker enters DEGRADED mode for a configurable window (default 24h). While
degraded:
- All promotions are blocked.
- Only strict-policy enforcement and retirement (safety actions) are allowed.
- A forced refreeze is scheduled at expiration.
- Daily reconciliation scans run.

Restore requires chain replay of governance_events to reconstruct state without
re-executing decisions.

This module is the sole writer of learning_mirror_circuit_breaker.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent import learning_constants as lc


@dataclass(frozen=True)
class CircuitBreakerState:
    breaker_id: str
    state: str
    activated_at: Optional[float]
    expires_at: Optional[float]
    reason: str
    governance_events_count: int
    previous_state: Optional[str]
    owner: str
    created_at: float


class LearningMirrorCircuitBreaker:
    """Circuit breaker for learning governance. Sole writer of breaker state table."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ──────────────────────────────
    # Public API
    # ──────────────────────────────

    def is_degraded(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        state = self._current_state()
        if state is None or state.state != lc.CIRCUIT_BREAKER_STATE_DEGRADED:
            return False
        if state.expires_at is not None and now >= state.expires_at:
            self._force_refreeze(now, state)
            return False  # after refreeze we are frozen/healthy with no promotion authority
        return True

    def activate(self, reason: str, now: Optional[float] = None) -> CircuitBreakerState:
        """Activate degraded mode. Idempotent if already degraded."""
        now = time.time() if now is None else now
        current = self._current_state()
        if current is not None and current.state == lc.CIRCUIT_BREAKER_STATE_DEGRADED:
            return current

        expires_at = now + (lc.CIRCUIT_BREAKER_DEGRADED_TIMEOUT_HOURS * 3600)
        previous_state = current.state if current else lc.CIRCUIT_BREAKER_STATE_HEALTHY
        state = CircuitBreakerState(
            breaker_id=str(uuid.uuid4()),
            state=lc.CIRCUIT_BREAKER_STATE_DEGRADED,
            activated_at=now,
            expires_at=expires_at,
            reason=reason,
            governance_events_count=self._event_count(),
            previous_state=previous_state,
            owner=lc.OWNER_LEARNING_GOVERNANCE,
            created_at=now,
        )
        self._insert_state(state)
        return state

    def restore(self, now: Optional[float] = None) -> Optional[CircuitBreakerState]:
        """Restore to healthy state after governance-chain replay verification.

        Returns None (refusing to restore) if the breaker is not degraded, or if
        the governance event chain fails integrity replay — restoring on a
        tampered/broken chain would re-enable promotions on untrustworthy state.
        """
        now = time.time() if now is None else now
        current = self._current_state()
        if current is None or current.state != lc.CIRCUIT_BREAKER_STATE_DEGRADED:
            return None

        # Chain replay: reconstruct/verify governance state without re-executing
        # decisions. Only restore if every strategy's hash-chain is intact.
        if not self._replay_governance_chain():
            return None

        state = CircuitBreakerState(
            breaker_id=str(uuid.uuid4()),
            state=lc.CIRCUIT_BREAKER_STATE_HEALTHY,
            activated_at=None,
            expires_at=None,
            reason="restore after degraded mode",
            governance_events_count=self._event_count(),
            previous_state=current.state,
            owner=lc.OWNER_LEARNING_GOVERNANCE,
            created_at=now,
        )
        self._insert_state(state)
        return state

    def current_state(self) -> Optional[CircuitBreakerState]:
        return self._current_state()

    def forced_refreeze_status(self, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Return status of forced refreeze if expired."""
        now = time.time() if now is None else now
        state = self._current_state()
        if state is None or state.state != lc.CIRCUIT_BREAKER_STATE_DEGRADED:
            return None
        expired = state.expires_at is not None and now >= state.expires_at
        return {
            "state": state.state,
            "expires_at": state.expires_at,
            "expired": expired,
            "reason": state.reason,
        }

    # ──────────────────────────────
    # Internal helpers
    # ──────────────────────────────

    def _current_state(self) -> Optional[CircuitBreakerState]:
        cur = self._conn.execute(
            """
            SELECT * FROM learning_mirror_circuit_breaker
            ORDER BY created_at DESC, breaker_id DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        return self._row_to_state(dict(row)) if row else None

    def _event_count(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) as n FROM learning_governance_events")
        return int(cur.fetchone()["n"])

    def _replay_governance_chain(self) -> bool:
        """Verify hash-chain integrity of every strategy's governance events.

        Returns True if all chains verify (or there is nothing to replay). Uses
        a lazy import of LearningGovernance to avoid a circular import at module
        load (learning_governance imports this breaker).
        """
        try:
            rows = self._conn.execute(
                "SELECT DISTINCT strategy_id FROM learning_governance_events"
            ).fetchall()
        except sqlite3.OperationalError:
            return True  # governance table absent — nothing to replay
        strategy_ids = [r[0] for r in rows if r and r[0] is not None]
        if not strategy_ids:
            return True
        from agent.learning_governance import LearningGovernance

        gov = LearningGovernance(self._conn)
        return all(gov.verify_chain(sid) for sid in strategy_ids)

    def _insert_state(self, state: CircuitBreakerState) -> None:
        self._conn.execute(
            """
            INSERT INTO learning_mirror_circuit_breaker (
                breaker_id, state, activated_at, expires_at, reason,
                governance_events_count, previous_state, owner, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.breaker_id,
                state.state,
                state.activated_at,
                state.expires_at,
                state.reason,
                state.governance_events_count,
                state.previous_state,
                state.owner,
                state.created_at,
            ),
        )
        self._conn.commit()

    def _force_refreeze(self, now: float, degraded_state: CircuitBreakerState) -> None:
        refrozen = CircuitBreakerState(
            breaker_id=str(uuid.uuid4()),
            state=lc.CIRCUIT_BREAKER_STATE_REFROZEN,
            activated_at=degraded_state.activated_at,
            expires_at=None,
            reason=f"forced refreeze after degraded mode expired: {degraded_state.reason}",
            governance_events_count=self._event_count(),
            previous_state=lc.CIRCUIT_BREAKER_STATE_DEGRADED,
            owner=lc.OWNER_LEARNING_GOVERNANCE,
            created_at=now,
        )
        self._insert_state(refrozen)

    def _row_to_state(self, row: Dict[str, Any]) -> CircuitBreakerState:
        return CircuitBreakerState(
            breaker_id=row["breaker_id"],
            state=row["state"],
            activated_at=row["activated_at"],
            expires_at=row["expires_at"],
            reason=row["reason"],
            governance_events_count=row["governance_events_count"],
            previous_state=row["previous_state"],
            owner=row["owner"],
            created_at=row["created_at"],
        )
