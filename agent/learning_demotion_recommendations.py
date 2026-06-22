"""Learning demotion recommendations queue for Learning Governance V1.

Provides the sole write path for the learning_demotion_recommendations table.
Monitors (such as AuthorityAlignmentMonitor) submit recommendations here; the
actual demotion decision is reserved for Milestone 3b LearningGovernance.

This module intentionally does NOT implement:
  - demotion decisions (Milestone 3b)
  - governance event recording (Milestone 3b)
  - strategy activation/deactivation (Milestone 3b)
"""
from __future__ import annotations

__all__ = ["LearningDemotionRecommendations", "DemotionRecommendation"]

import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent import learning_constants as lc


@dataclass(frozen=True)
class DemotionRecommendation:
    """A demotion recommendation queued for future governance review."""

    rec_id: str
    monitor_type: str
    strategy_id: str
    reason: str
    created_at: float


class LearningDemotionRecommendations:
    """Sole writer of learning_demotion_recommendations.

    Provides idempotent enqueue, poll for unprocessed recommendations, and
    expiry handling. No demotion decisions are made here.
    """

    def __init__(self, store_conn: sqlite3.Connection):
        self._conn = store_conn

    def enqueue(
        self,
        monitor_type: str,
        strategy_id: str,
        reason: str,
        rec_id: Optional[str] = None,
        created_at: Optional[float] = None,
    ) -> str:
        """Add a demotion recommendation to the queue."""
        rec_id = rec_id or str(uuid.uuid4())
        created_at = created_at or time.time()
        with self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO learning_demotion_recommendations
                (rec_id, monitor_type, strategy_id, reason, created_at, processed_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (rec_id, monitor_type, strategy_id, reason, created_at),
            )
        return rec_id

    def enqueue_directive(
        self,
        monitor_type: str,
        strategy_id: str,
        directive: Any,
        reason: str,
    ) -> str:
        """Convenience: enqueue from a directive-like object with knob/value."""
        # Extract value from object if it has a value attribute; otherwise serialize reason
        detailed_reason = reason
        if hasattr(directive, "knob") and hasattr(directive, "value"):
            detailed_reason = f"{reason}; directive: {directive.knob}={directive.value}"
        return self.enqueue(monitor_type, strategy_id, detailed_reason)

    def mark_processed(self, rec_id: str, processed_at: Optional[float] = None) -> bool:
        """Mark a single recommendation as processed."""
        processed_at = processed_at or time.time()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE learning_demotion_recommendations SET processed_at=? WHERE rec_id=? AND processed_at IS NULL",
                (processed_at, rec_id),
            )
        return cur.rowcount > 0

    def mark_processed_for_strategy(self, strategy_id: str) -> int:
        """Mark all unprocessed recommendations for a strategy as processed."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE learning_demotion_recommendations SET processed_at=? WHERE strategy_id=? AND processed_at IS NULL",
                (time.time(), strategy_id),
            )
        return cur.rowcount

    def get_unprocessed(self, strategy_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Return raw unprocessed, non-expired recommendations as dicts."""
        cutoff = time.time() - (lc.DEMOTION_RECOMMENDATION_MAX_AGE_DAYS * 24 * 3600)
        if strategy_id:
            cur = self._conn.execute(
                """
                SELECT * FROM learning_demotion_recommendations
                WHERE strategy_id=? AND processed_at IS NULL AND created_at >= ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (strategy_id, cutoff, limit),
            )
        else:
            cur = self._conn.execute(
                """
                SELECT * FROM learning_demotion_recommendations
                WHERE processed_at IS NULL AND created_at >= ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (cutoff, limit),
            )
        return [dict(r) for r in cur.fetchall()]

    def poll(self, strategy_id: Optional[str] = None, limit: int = 100) -> List[DemotionRecommendation]:
        """Return unprocessed, non-expired recommendations as dataclasses."""
        rows = self.get_unprocessed(strategy_id, limit)
        return [self._row_to_rec(r) for r in rows]

    def expire_recommendations(self) -> int:
        """Remove recommendations older than DEMOTION_RECOMMENDATION_MAX_AGE_DAYS."""
        cutoff = time.time() - (lc.DEMOTION_RECOMMENDATION_MAX_AGE_DAYS * 24 * 3600)
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM learning_demotion_recommendations WHERE created_at < ?",
                (cutoff,),
            )
            return cur.rowcount

    def get_unprocessed_count(self, strategy_id: Optional[str] = None) -> int:
        if strategy_id:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM learning_demotion_recommendations WHERE strategy_id=? AND processed_at IS NULL",
                (strategy_id,),
            )
        else:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM learning_demotion_recommendations WHERE processed_at IS NULL"
            )
        row = cur.fetchone()
        return row[0] if row else 0

    def get_all(self, strategy_id: Optional[str] = None) -> List[DemotionRecommendation]:
        if strategy_id:
            cur = self._conn.execute(
                "SELECT * FROM learning_demotion_recommendations WHERE strategy_id=? ORDER BY created_at ASC",
                (strategy_id,),
            )
        else:
            cur = self._conn.execute("SELECT * FROM learning_demotion_recommendations ORDER BY created_at ASC")
        return [self._row_to_rec(dict(r)) for r in cur.fetchall()]

    def get_all_count(self, strategy_id: Optional[str] = None) -> int:
        if strategy_id:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM learning_demotion_recommendations WHERE strategy_id=?",
                (strategy_id,),
            )
        else:
            cur = self._conn.execute("SELECT COUNT(*) FROM learning_demotion_recommendations")
        row = cur.fetchone()
        return row[0] if row else 0

    @staticmethod
    def _row_to_rec(row: Dict[str, Any]) -> DemotionRecommendation:
        return DemotionRecommendation(
            rec_id=row["rec_id"],
            monitor_type=row["monitor_type"],
            strategy_id=row["strategy_id"],
            reason=row["reason"],
            created_at=row["created_at"],
        )

    @property
    def table_name(self) -> str:
        return "learning_demotion_recommendations"
