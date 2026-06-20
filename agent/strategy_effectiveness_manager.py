"""Strategy effectiveness manager for Learning Governance V1.

Computes per-strategy effectiveness metrics from learning_strategy_observations
and learning_cases. Returns promotion/retirement eligibility assessments but
does NOT perform governance actions.

This module intentionally does NOT implement:
  - promotion or retirement decisions (Milestone 3b)
  - governance event recording (Milestone 3b)
  - drift monitoring (Milestone 3b)
"""
from __future__ import annotations

__all__ = ["StrategyEffectivenessManager", "EffectivenessResult"]

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent import learning_constants as lc


@dataclass(frozen=True)
class EffectivenessResult:
    """Read-only effectiveness metrics for a single strategy."""

    strategy_id: str
    sample_count: int
    win_rate: float
    avg_score: float
    avg_alignment: float
    avg_drift: float
    promotion_eligible: bool
    retirement_eligible: bool


class StrategyEffectivenessManager:
    """Evaluate strategy effectiveness from evidence.

    This component is read-only. It returns metrics and eligibility flags but
    never modifies authority, strategies, or governance state.
    """

    def __init__(self, store_conn: sqlite3.Connection):
        self._conn = store_conn

    def _fetch_observations(self, strategy_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if strategy_id:
            cur = self._conn.execute(
                "SELECT * FROM learning_strategy_observations "
                "WHERE strategy_id=? AND owner=? AND contract_version=?",
                (strategy_id, lc.OWNER_LEARNING_GOVERNANCE, lc.OPVAL_EVIDENCE_CONTRACT_VERSION),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM learning_strategy_observations "
                "WHERE owner=? AND contract_version=?",
                (lc.OWNER_LEARNING_GOVERNANCE, lc.OPVAL_EVIDENCE_CONTRACT_VERSION),
            )
        return [dict(r) for r in cur.fetchall()]

    def _extract_metrics(self, observations: List[Dict[str, Any]]) -> Dict[str, float]:
        """Aggregate metrics from observation payloads."""
        if not observations:
            return {
                "sample_count": 0,
                "win_rate": 0.0,
                "avg_score": 0.0,
                "avg_alignment": 0.0,
                "avg_drift": 0.0,
            }

        total = len(observations)
        wins = sum(1 for o in observations if o.get("outcome") == lc.FEEDBACK_OUTCOME_SUCCESS)
        scores: List[float] = []
        alignments: List[float] = []
        drifts: List[float] = []

        for obs in observations:
            payload = self._parse_payload(obs.get("payload_json", "{}"))
            # Prefer session-level aggregate values when present
            promotion_score = payload.get("promotion_score") or payload.get("quality_score") or 0.0
            drift_pct = payload.get("drift_pct") or 0.0
            misalignment_pct = payload.get("misalignment_pct") or 0.0
            score = float(promotion_score) * 100.0
            # Alignment derived from 1 - misalignment, clamped
            alignment = max(0.0, 1.0 - float(misalignment_pct)) * 100.0
            scores.append(score)
            alignments.append(alignment)
            drifts.append(float(drift_pct))

        return {
            "sample_count": total,
            "win_rate": wins / total if total else 0.0,
            "avg_score": sum(scores) / len(scores) if scores else 0.0,
            "avg_alignment": sum(alignments) / len(alignments) if alignments else 0.0,
            "avg_drift": sum(drifts) / len(drifts) if drifts else 0.0,
        }

    @staticmethod
    def _parse_payload(payload_json: Optional[str]) -> Dict[str, Any]:
        import json
        if not payload_json:
            return {}
        try:
            return json.loads(payload_json)
        except Exception:
            return {}

    def evaluate(self, strategy_id: str) -> EffectivenessResult:
        """Compute effectiveness result for a single strategy.

        Promotion eligibility requires:
          - sample_count >= EFFECTIVENESS_MIN_SAMPLE_COUNT
          - win_rate >= EFFECTIVENESS_WIN_RATE_THRESHOLD
          - avg_score >= EFFECTIVENESS_AVG_SCORE_THRESHOLD
          - avg_alignment >= EFFECTIVENESS_AVG_ALIGNMENT_THRESHOLD
          - avg_drift <= LEARNING_PROMOTION_MAX_DRIFT_PERCENT

        Retirement eligibility requires:
          - sample_count >= EFFECTIVENESS_MIN_SAMPLE_COUNT
          - win_rate <= RETIREMENT_WIN_RATE
        """
        observations = self._fetch_observations(strategy_id)
        metrics = self._extract_metrics(observations)

        n = int(metrics["sample_count"])
        win_rate = float(metrics["win_rate"])
        avg_score = float(metrics["avg_score"])
        avg_alignment = float(metrics["avg_alignment"])
        avg_drift = float(metrics["avg_drift"])

        sufficient_samples = n >= lc.EFFECTIVENESS_MIN_SAMPLE_COUNT

        promotion_eligible = (
            sufficient_samples
            and win_rate >= lc.EFFECTIVENESS_WIN_RATE_THRESHOLD
            and avg_score >= lc.EFFECTIVENESS_AVG_SCORE_THRESHOLD
            and avg_alignment >= lc.EFFECTIVENESS_AVG_ALIGNMENT_THRESHOLD
            and avg_drift <= lc.LEARNING_PROMOTION_MAX_DRIFT_PERCENT
        )

        retirement_eligible = (
            sufficient_samples
            and win_rate <= lc.RETIREMENT_WIN_RATE
        )

        return EffectivenessResult(
            strategy_id=strategy_id,
            sample_count=n,
            win_rate=win_rate,
            avg_score=avg_score,
            avg_alignment=avg_alignment,
            avg_drift=avg_drift,
            promotion_eligible=promotion_eligible,
            retirement_eligible=retirement_eligible,
        )

    def evaluate_all(self) -> List[EffectivenessResult]:
        """Compute effectiveness for every strategy with at least one observation."""
        cur = self._conn.execute(
            "SELECT DISTINCT strategy_id FROM learning_strategy_observations WHERE owner=?",
            (lc.OWNER_LEARNING_GOVERNANCE,),
        )
        strategy_ids = [r["strategy_id"] for r in cur.fetchall()]
        return [self.evaluate(sid) for sid in strategy_ids]

    def get_strategy_summary(self, strategy_id: str) -> Dict[str, Any]:
        """Return a serializable summary without eligibility flags."""
        result = self.evaluate(strategy_id)
        return {
            "strategy_id": result.strategy_id,
            "sample_count": result.sample_count,
            "win_rate": result.win_rate,
            "avg_score": result.avg_score,
            "avg_alignment": result.avg_alignment,
            "avg_drift": result.avg_drift,
        }
