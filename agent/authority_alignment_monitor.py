"""Authority alignment monitor for Learning Governance V1.

Monitors runtime authority decisions and strategy effectiveness, then emits
StrategicDirective recommendations when misalignment is detected. This module
never applies authority, never promotes/retires strategies, and never records
governance events.

This module intentionally does NOT implement:
  - direct authority application (Milestone 3b)
  - governance event recording (Milestone 3b)
  - strategy activation (Milestone 3b)
"""
from __future__ import annotations

__all__ = ["AuthorityAlignmentMonitor", "AlignmentRecommendation"]

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent import learning_constants as lc
from agent.runtime_authority import AuthorityDecision
from agent.strategic_learning import StrategicDirective, build_authority_directive
from agent.strategy_effectiveness_manager import StrategyEffectivenessManager


@dataclass(frozen=True)
class AlignmentRecommendation:
    """A recommendation emitted by the alignment monitor."""

    rec_id: str
    strategy_id: str
    monitor_type: str
    reason: str
    directive: Optional[StrategicDirective]
    confidence: float


class AuthorityAlignmentMonitor:
    """Watch authority decisions and strategy effectiveness for alignment.

    Emits recommendations only. All recommendations are validated through
    build_authority_directive so only allowed authority knobs are used.
    """

    MONITOR_TYPE = "authority_alignment"

    def __init__(
        self,
        store_conn: sqlite3.Connection,
        effectiveness_manager: StrategyEffectivenessManager,
    ):
        self._conn = store_conn
        self._effectiveness = effectiveness_manager

    def _recent_authority_decisions(self, strategy_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Read recent authority decision cases for a strategy from learning_cases."""
        cur = self._conn.execute(
            "SELECT * FROM learning_cases WHERE strategy_id=? ORDER BY created_at DESC LIMIT ?",
            (strategy_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def _parse_directive(self, directive_json: str) -> Optional[Dict[str, Any]]:
        import json
        try:
            return json.loads(directive_json)
        except Exception:
            return None

    def _build_safe_directive(
        self,
        strategy_id: str,
        knob: str,
        value: Any,
        reason: str,
    ) -> StrategicDirective:
        """Wrap a recommendation in StrategicDirective with allowed-knob validation."""
        if knob in lc.FORBIDDEN_AUTHORITY_KNOBS:
            raise ValueError(f"AuthorityAlignmentMonitor cannot recommend forbidden knob: {knob}")
        if knob not in lc.ALLOWED_AUTHORITY_KNOBS:
            raise ValueError(f"Unknown authority knob: {knob}")

        # Determine a reasonable confidence based on knob type
        confidence = 0.8
        return StrategicDirective(
            strategy_id=strategy_id,
            knob=knob,
            value=value,
            confidence=confidence,
            reason=reason,
        )

    def check_alignment(self, strategy_id: str) -> List[AlignmentRecommendation]:
        """Check whether recent authority decisions align with effectiveness.

        Returns recommendations only; never mutates authority or governance.
        """
        recommendations: List[AlignmentRecommendation] = []

        # Load effectiveness for the strategy
        effectiveness = self._effectiveness.evaluate(strategy_id)

        # If strategy is performing poorly, recommend a stricter policy
        if (
            effectiveness.sample_count >= lc.EFFECTIVENESS_MIN_SAMPLE_COUNT
            and effectiveness.win_rate < lc.EFFECTIVENESS_WIN_RATE_THRESHOLD
        ):
            directive = self._build_safe_directive(
                strategy_id=strategy_id,
                knob="policy",
                value=lc.AUTHORITY_POLICY_STRICT,
                reason=f"win_rate {effectiveness.win_rate:.2%} below threshold {lc.EFFECTIVENESS_WIN_RATE_THRESHOLD:.2%}",
            )
            recommendations.append(
                AlignmentRecommendation(
                    rec_id=str(uuid.uuid4()),
                    strategy_id=strategy_id,
                    monitor_type=self.MONITOR_TYPE,
                    reason=directive.reason,
                    directive=directive,
                    confidence=directive.confidence,
                )
            )

        # If average drift is high, recommend a tighter tool scope
        if (
            effectiveness.sample_count >= lc.EFFECTIVENESS_MIN_SAMPLE_COUNT
            and effectiveness.avg_drift > lc.LEARNING_PROMOTION_MAX_DRIFT_PERCENT
        ):
            directive = self._build_safe_directive(
                strategy_id=strategy_id,
                knob="tool_scope",
                value=("terminal", "delegate"),
                reason=f"avg_drift {effectiveness.avg_drift:.2%} exceeds promotion drift limit {lc.LEARNING_PROMOTION_MAX_DRIFT_PERCENT:.2%}",
            )
            recommendations.append(
                AlignmentRecommendation(
                    rec_id=str(uuid.uuid4()),
                    strategy_id=strategy_id,
                    monitor_type=self.MONITOR_TYPE,
                    reason=directive.reason,
                    directive=directive,
                    confidence=directive.confidence,
                )
            )

        return recommendations

    def emit_directive_dicts(self, strategy_id: str) -> List[Dict[str, Any]]:
        """Return a list of validated directive dicts for downstream recording.

        This method validates every recommendation through build_authority_directive.
        """
        recommendations = self.check_alignment(strategy_id)
        result = []
        for rec in recommendations:
            if rec.directive is None:
                continue
            directive = build_authority_directive(
                rec.directive.strategy_id,
                rec.directive.knob,
                rec.directive.value,
                rec.directive.confidence,
                rec.directive.reason,
            )
            result.append({
                "rec_id": rec.rec_id,
                "strategy_id": rec.strategy_id,
                "monitor_type": rec.monitor_type,
                "reason": rec.reason,
                "confidence": rec.confidence,
                "directive": {
                    "knob": rec.directive.knob,
                    "value": rec.directive.value,
                },
                "authority_decision_valid": directive is not None,
            })
        return result
