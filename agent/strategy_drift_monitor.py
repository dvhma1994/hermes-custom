"""Strategy drift monitor for Learning Governance V1.

Captures periodic snapshots of observed strategy behavior and compares them
against baseline metrics (from StrategyEffectivenessManager). Drift is computed
as the absolute change in win_rate relative to a recent baseline.

This module is read-only from evidence tables and only writes to
learning_drift_snapshots.
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent import learning_constants as lc
from agent.strategy_effectiveness_manager import StrategyEffectivenessManager


@dataclass(frozen=True)
class DriftSnapshot:
    snapshot_id: str
    strategy_id: str
    drift_pct: float
    misalignment_pct: float
    win_rate: float
    avg_score: float
    avg_alignment: float
    sample_count: int
    threshold_pct: float
    owner: str
    created_at: float


class StrategyDriftMonitor:
    """Drift monitor. Sole writer of learning_drift_snapshots."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._effectiveness = StrategyEffectivenessManager(conn)

    def snapshot(self, strategy_id: str, now: Optional[float] = None) -> DriftSnapshot:
        """Record a drift snapshot for a strategy."""
        import time

        now = now or time.time()
        current = self._effectiveness.evaluate(strategy_id)

        baseline = self._baseline_win_rate(strategy_id)
        drift_pct = abs(current.win_rate - baseline) if baseline is not None else 0.0

        snapshot = DriftSnapshot(
            snapshot_id=str(uuid.uuid4()),
            strategy_id=strategy_id,
            drift_pct=drift_pct,
            misalignment_pct=max(0.0, 1.0 - current.avg_alignment / 100.0),
            win_rate=current.win_rate,
            avg_score=current.avg_score,
            avg_alignment=current.avg_alignment,
            sample_count=current.sample_count,
            threshold_pct=lc.LEARNING_PROMOTION_MAX_DRIFT_PERCENT,
            owner=lc.OWNER_LEARNING_GOVERNANCE,
            created_at=now,
        )

        self._conn.execute(
            """
            INSERT INTO learning_drift_snapshots (
                snapshot_id, strategy_id, drift_pct, misalignment_pct, win_rate,
                avg_score, avg_alignment, sample_count, threshold_pct, owner, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.snapshot_id,
                snapshot.strategy_id,
                snapshot.drift_pct,
                snapshot.misalignment_pct,
                snapshot.win_rate,
                snapshot.avg_score,
                snapshot.avg_alignment,
                snapshot.sample_count,
                snapshot.threshold_pct,
                snapshot.owner,
                snapshot.created_at,
            ),
        )
        self._conn.commit()
        return snapshot

    def is_drift_alert(self, strategy_id: str, now: Optional[float] = None) -> bool:
        """True if the strategy's current win_rate has drifted beyond threshold.

        READ-ONLY: this no longer records a snapshot. ``_baseline_win_rate`` is
        the AVG over recorded snapshots, and ``snapshot()`` inserts one with the
        CURRENT win_rate — so polling drift via snapshot() on every call dragged
        the baseline toward the current value and self-extinguished a real
        sustained drift (a 0.9→0.4 shift faded to ~0 just from being polled).
        Compute drift against the existing baseline without mutating it; call
        ``snapshot()`` explicitly to record a new baseline sample.
        """
        current = self._effectiveness.evaluate(strategy_id)
        baseline = self._baseline_win_rate(strategy_id)
        if baseline is None:
            return False
        drift_pct = abs(current.win_rate - baseline)
        return drift_pct > lc.LEARNING_PROMOTION_MAX_DRIFT_PERCENT

    def recent_snapshots(self, strategy_id: str, limit: int = 10) -> List[DriftSnapshot]:
        cur = self._conn.execute(
            """
            SELECT * FROM learning_drift_snapshots
            WHERE strategy_id=? ORDER BY created_at DESC
            LIMIT ?
            """,
            (strategy_id, limit),
        )
        return [self._row_to_snapshot(dict(r)) for r in cur.fetchall()]

    def _baseline_win_rate(self, strategy_id: str) -> Optional[float]:
        """Baseline = average win_rate over snapshots within max age window."""
        import time

        cutoff = time.time() - (lc.DRIFT_MAX_SNAPSHOT_AGE_DAYS * 86400)
        cur = self._conn.execute(
            """
            SELECT AVG(win_rate) as baseline FROM learning_drift_snapshots
            WHERE strategy_id=? AND created_at >= ?
            """,
            (strategy_id, cutoff),
        )
        row = cur.fetchone()
        return float(row["baseline"]) if row and row["baseline"] is not None else None

    def _row_to_snapshot(self, row: Dict[str, Any]) -> DriftSnapshot:
        return DriftSnapshot(
            snapshot_id=row["snapshot_id"],
            strategy_id=row["strategy_id"],
            drift_pct=row["drift_pct"],
            misalignment_pct=row["misalignment_pct"],
            win_rate=row["win_rate"],
            avg_score=row["avg_score"],
            avg_alignment=row["avg_alignment"],
            sample_count=row["sample_count"],
            threshold_pct=row["threshold_pct"],
            owner=row["owner"],
            created_at=row["created_at"],
        )
