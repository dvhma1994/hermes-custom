"""Learning governance engine for Learning Governance V1.

This is the sole authority for promotion, retirement, and enforcement decisions.
Every decision is recorded as an append-only event in learning_governance_events
before any downstream application. The engine consumes read-only outputs from
Milestone 3a (effectiveness, alignment, demotion recommendations) and checks
circuit-breaker state from LearningMirrorCircuitBreaker.

Constraints enforced:
- Promotion requires M3a eligibility plus no degraded mode.
- Retirement requires M3a eligibility or an unprocessed demotion recommendation.
- Enforcement only mutates allowed authority knobs and records a governance event.
- All governance events are append-only; no UPDATE/DELETE on learning_governance_events.
- M3b does not implement Milestone 4 functionality.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from agent import learning_constants as lc
from agent.authority_alignment_monitor import AuthorityAlignmentMonitor, AlignmentRecommendation
from agent.learning_demotion_recommendations import LearningDemotionRecommendations
from agent.learning_mirror_circuit_breaker import LearningMirrorCircuitBreaker
from agent.runtime_authority import AuthorityDecision, RuntimeAuthority
from agent.strategy_effectiveness_manager import EffectivenessResult, StrategyEffectivenessManager


@dataclass(frozen=True)
class GovernanceEvent:
    event_id: str
    event_type: str
    strategy_id: str
    decision_reason: str
    previous_hash: Optional[str]
    source_hash: str
    effectiveness_result_json: Optional[str]
    authority_directive_json: Optional[str]
    status: str
    owner: str
    created_at: float


@dataclass(frozen=True)
class GovernanceDecision:
    approved: bool
    reason: str
    event: Optional[GovernanceEvent] = None


class LearningGovernance:
    """Central governance authority. Sole writer of learning_governance_events."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        effectiveness: Optional[StrategyEffectivenessManager] = None,
        alignment: Optional[AuthorityAlignmentMonitor] = None,
        breaker: Optional[LearningMirrorCircuitBreaker] = None,
    ) -> None:
        self._conn = conn
        self._effectiveness = effectiveness or StrategyEffectivenessManager(conn)
        self._alignment = alignment or AuthorityAlignmentMonitor(conn, self._effectiveness)
        self._breaker = breaker or LearningMirrorCircuitBreaker(conn)

    # ──────────────────────────────
    # Public API
    # ──────────────────────────────

    def promote_strategy(self, strategy_id: str, override_reason: Optional[str] = None) -> GovernanceDecision:
        """Record a promotion decision if all gates pass."""
        if self._breaker.is_degraded():
            return GovernanceDecision(False, "Promotion blocked: circuit breaker is in degraded mode")

        eff = self._effectiveness.evaluate(strategy_id)
        if not eff.promotion_eligible:
            return GovernanceDecision(False, f"Promotion rejected: effectiveness not eligible ({eff})")

        drift_pct = eff.avg_drift
        if drift_pct > lc.LEARNING_PROMOTION_MAX_DRIFT_PERCENT:
            return GovernanceDecision(False, f"Promotion rejected: drift {drift_pct} exceeds threshold")

        alignment_recs = self._alignment.check_alignment(strategy_id)
        if any(r.is_critical for r in alignment_recs):
            return GovernanceDecision(False, "Promotion rejected: critical alignment recommendation present")

        recs = self._demotion_recommendations_for(strategy_id)
        if recs:
            return GovernanceDecision(False, "Promotion rejected: active retirement/demotion recommendation")

        directive = self._build_promotion_directive(strategy_id, eff)
        reason = override_reason or (
            f"promotion eligible: win_rate={eff.win_rate:.2f}, avg_score={eff.avg_score:.1f}, "
            f"avg_alignment={eff.avg_alignment:.1f}, sample_count={eff.sample_count}"
        )
        event = self._record_event(
            event_type=lc.GOVERNANCE_EVENT_PROMOTE,
            strategy_id=strategy_id,
            decision_reason=reason,
            effectiveness=eff,
            directive=directive,
        )
        return GovernanceDecision(True, "Promotion recorded", event=event)

    def retire_strategy(self, strategy_id: str, override_reason: Optional[str] = None) -> GovernanceDecision:
        """Record a retirement decision if effectiveness or demotion queue requires it."""
        eff = self._effectiveness.evaluate(strategy_id)
        recs = self._demotion_recommendations_for(strategy_id)

        if not eff.retirement_eligible and not recs:
            return GovernanceDecision(False, "Retirement rejected: no effectiveness or demotion trigger")

        reason = override_reason
        if not reason:
            if eff.retirement_eligible:
                reason = (
                    f"retirement eligible: win_rate={eff.win_rate:.2f}, avg_score={eff.avg_score:.1f}, "
                    f"sample_count={eff.sample_count}"
                )
            else:
                reason = f"retirement from demotion queue: {len(recs)} recommendation(s)"

        directive = self._build_retirement_directive(strategy_id, eff)
        event = self._record_event(
            event_type=lc.GOVERNANCE_EVENT_RETIRE,
            strategy_id=strategy_id,
            decision_reason=reason,
            effectiveness=eff,
            directive=directive,
        )
        if recs:
            self._mark_demotion_processed(strategy_id)
        return GovernanceDecision(True, "Retirement recorded", event=event)

    def enforce_policy(
        self,
        strategy_id: str,
        directive: AuthorityDecision,
        reason: str,
    ) -> GovernanceDecision:
        """Record an enforcement event for an allowed authority directive."""
        if self._breaker.is_degraded():
            # Degraded mode allows ONLY a strict-policy safety action: policy must
            # be STRICT and no other (non-policy) knob may ride along. This stops
            # a degraded breaker from being used to push tool_scope / context /
            # iteration changes under the cover of a strict policy.
            other_knob_set = any(
                getattr(directive, knob) is not None
                for knob in lc.ALLOWED_AUTHORITY_KNOBS
                if knob != "policy"
            )
            if directive.policy != lc.AUTHORITY_POLICY_STRICT or other_knob_set:
                return GovernanceDecision(
                    False,
                    "Enforcement blocked in degraded mode except a strict-policy-only safety action",
                )

        for key, value in asdict(directive).items():
            if key not in lc.ALLOWED_AUTHORITY_KNOBS and value is not None:
                return GovernanceDecision(False, f"Enforcement rejected: knob '{key}' is not allowed")

        if self._breaker.is_degraded():
            reason = f"[DEGRADED] {reason}"

        event = self._record_event(
            event_type=lc.GOVERNANCE_EVENT_ENFORCE,
            strategy_id=strategy_id,
            decision_reason=reason,
            directive=directive,
        )
        return GovernanceDecision(True, "Enforcement recorded", event=event)

    def replay_events(self, strategy_id: str) -> List[GovernanceEvent]:
        """Return all governance events for a strategy in chronological order."""
        cur = self._conn.execute(
            """
            SELECT * FROM learning_governance_events
            WHERE strategy_id=?
            ORDER BY created_at ASC, rowid ASC
            """,
            (strategy_id,),
        )
        return [self._row_to_event(dict(r)) for r in cur.fetchall()]

    def verify_chain(self, strategy_id: str) -> bool:
        """Verify hash-chain integrity for a strategy's governance events."""
        events = self.replay_events(strategy_id)
        if not events:
            return True  # Empty chain is valid
        prev_event = None
        for i, event in enumerate(events):
            # Linkage check: the genesis event must have no predecessor; every
            # later event's stored previous_hash must equal the prior event's
            # stored source_hash.
            expected_prev = None if i == 0 else prev_event.source_hash
            if event.previous_hash != expected_prev:
                return False
            # Recompute the source_hash from the event's OWN stored
            # previous_hash (not a separately tracked local) so that tampering
            # with previous_hash — including on the genesis event — is detected.
            expected = self._compute_hash(event, event.previous_hash)
            if event.source_hash != expected:
                return False
            prev_event = event
        return True

    # ──────────────────────────────
    # Internal helpers
    # ──────────────────────────────

    def _demotion_recommendations_for(self, strategy_id: str) -> List[Dict[str, Any]]:
        queue = LearningDemotionRecommendations(self._conn)
        return queue.get_unprocessed(strategy_id)

    def _mark_demotion_processed(self, strategy_id: str) -> None:
        queue = LearningDemotionRecommendations(self._conn)
        queue.mark_processed_for_strategy(strategy_id)

    def _build_promotion_directive(self, strategy_id: str, eff: EffectivenessResult) -> AuthorityDecision:
        # Promotion raises context_size_override slightly as a reward signal.
        return AuthorityDecision(
            policy=lc.AUTHORITY_POLICY_PERMISSIVE,
            tool_scope=None,
            context_size_override=min(16000, int(8000 + eff.avg_score * 50)),
            allow_self_delegate=True,
            max_tool_iterations=lc.DEFAULT_MAX_TOOL_ITERATIONS,
        )

    def _build_retirement_directive(self, strategy_id: str, eff: EffectivenessResult) -> AuthorityDecision:
        return AuthorityDecision(
            policy=lc.AUTHORITY_POLICY_STRICT,
            tool_scope=["allowed_tools"],
            context_size_override=4000,
            allow_self_delegate=False,
            max_tool_iterations=10,
        )

    def _record_event(
        self,
        event_type: str,
        strategy_id: str,
        decision_reason: str,
        effectiveness: Optional[EffectivenessResult] = None,
        directive: Optional[AuthorityDecision] = None,
    ) -> GovernanceEvent:
        previous_hash = self._last_hash(strategy_id)
        event_id = str(uuid.uuid4())
        now = self._now()
        eff_json = json.dumps(asdict(effectiveness)) if effectiveness else None
        dir_json = json.dumps(asdict(directive)) if directive else None

        event = GovernanceEvent(
            event_id=event_id,
            event_type=event_type,
            strategy_id=strategy_id,
            decision_reason=decision_reason,
            previous_hash=previous_hash,
            source_hash="",  # computed below
            effectiveness_result_json=eff_json,
            authority_directive_json=dir_json,
            status=lc.GOVERNANCE_STATUS_PENDING,
            owner=lc.OWNER_LEARNING_GOVERNANCE,
            created_at=now,
        )
        source_hash = self._compute_hash(event, previous_hash)
        event = GovernanceEvent(
            event_id=event_id,
            event_type=event_type,
            strategy_id=strategy_id,
            decision_reason=decision_reason,
            previous_hash=previous_hash,
            source_hash=source_hash,
            effectiveness_result_json=eff_json,
            authority_directive_json=dir_json,
            status=lc.GOVERNANCE_STATUS_PENDING,
            owner=lc.OWNER_LEARNING_GOVERNANCE,
            created_at=now,
        )

        self._conn.execute(
            """
            INSERT INTO learning_governance_events (
                event_id, event_type, strategy_id, decision_reason, previous_hash, source_hash,
                effectiveness_result_json, authority_directive_json, status, owner, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.event_type,
                event.strategy_id,
                event.decision_reason,
                event.previous_hash,
                event.source_hash,
                event.effectiveness_result_json,
                event.authority_directive_json,
                event.status,
                event.owner,
                event.created_at,
            ),
        )
        self._conn.commit()
        return event

    def _last_hash(self, strategy_id: str) -> Optional[str]:
        cur = self._conn.execute(
            "SELECT source_hash FROM learning_governance_events WHERE strategy_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (strategy_id,),
        )
        row = cur.fetchone()
        return row["source_hash"] if row else None

    def _compute_hash(self, event: GovernanceEvent, previous_hash: Optional[str]) -> str:
        payload = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "strategy_id": event.strategy_id,
            "decision_reason": event.decision_reason,
            "previous_hash": previous_hash,
            "effectiveness_result_json": event.effectiveness_result_json,
            "authority_directive_json": event.authority_directive_json,
            "status": event.status,
            "owner": event.owner,
            "created_at": event.created_at,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _row_to_event(self, row: Dict[str, Any]) -> GovernanceEvent:
        return GovernanceEvent(**{k: row.get(k) for k in GovernanceEvent.__annotations__})

    def _now(self) -> float:
        import time

        return time.time()
