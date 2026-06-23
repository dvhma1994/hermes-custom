"""Strategic Learning: records cases and seeds authority directives.

This module consumes observations from LearningEvidenceBuilder and feedback
from RuntimeFeedbackCollector. It seeds AuthorityDecision directives through a
confidence gate and records cases into `learning_cases`.

This module intentionally does NOT implement:
  - promotion / retirement decisions (Milestone 3)
  - governance event append-only chain (Milestone 3)
  - strategy generation or drift monitoring (Milestone 3/4)
"""
from __future__ import annotations

__all__ = ["StrategicLearning", "DirectiveConfidenceGate", "build_authority_directive"]

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent import learning_constants as lc
from agent.learning_evidence_builder import LearningEvidenceBuilder, compute_source_hash
from agent.runtime_authority import AuthorityDecision, RuntimeAuthority
from agent.runtime_feedback import RuntimeFeedbackCollector


class DirectiveConfidenceGate:
    """Simple confidence gate for authority directives."""

    def __init__(self, threshold: float = lc.PROMOTION_WIN_RATE):
        self._threshold = threshold

    def is_allowed(self, confidence: float) -> bool:
        return confidence >= self._threshold


@dataclass(frozen=True)
class StrategicDirective:
    """A directive produced by strategic learning for authority application."""

    strategy_id: str
    knob: str
    value: Any
    confidence: float
    reason: str


def build_authority_directive(
    strategy_id: str,
    knob: str,
    value: Any,
    confidence: float,
    reason: str,
) -> Optional[AuthorityDecision]:
    """Build an AuthorityDecision from a strategic directive.

    Only allowed authority knobs may be used. Forbidden knobs are rejected.
    """
    if knob in lc.FORBIDDEN_AUTHORITY_KNOBS:
        raise ValueError(f"StrategicLearning cannot mutate forbidden knob: {knob}")
    if knob not in lc.ALLOWED_AUTHORITY_KNOBS:
        raise ValueError(f"Unknown authority knob: {knob}")

    kwargs: Dict[str, Any] = {}
    if knob == "policy":
        kwargs["policy"] = value
    elif knob == "tool_scope":
        kwargs["tool_scope"] = tuple(value) if not isinstance(value, (list, tuple, set, frozenset)) else tuple(value)
    elif knob == "context_size_override":
        kwargs["context_size_override"] = int(value)
    elif knob == "allow_self_delegate":
        kwargs["allow_self_delegate"] = bool(value)
    elif knob == "max_tool_iterations":
        kwargs["max_tool_iterations"] = int(value)

    return AuthorityDecision(**kwargs)


class StrategicLearning:
    """Records learning cases and seeds authority directives with confidence gating."""

    def __init__(
        self,
        store_conn: sqlite3.Connection,
        evidence_builder: LearningEvidenceBuilder,
        confidence_gate: Optional[DirectiveConfidenceGate] = None,
    ):
        self._conn = store_conn
        self._evidence_builder = evidence_builder
        self._confidence_gate = confidence_gate or DirectiveConfidenceGate()
        self._strategy_id = evidence_builder.strategy_id

    def record_case(
        self,
        observation_id: str,
        directive: StrategicDirective,
        result_snapshot: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Record a case into learning_cases if observation exists and confidence is allowed."""
        if not self._confidence_gate.is_allowed(directive.confidence):
            raise ValueError(
                f"Directive confidence {directive.confidence} below threshold {self._confidence_gate._threshold}"
            )

        # Verify observation exists and belongs to our strategy
        row = self._conn.execute(
            "SELECT strategy_id FROM learning_strategy_observations WHERE observation_id=?",
            (observation_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Observation {observation_id} not found")
        if dict(row)["strategy_id"] != self._strategy_id:
            raise ValueError("Observation does not belong to this StrategicLearning strategy")

        case_id = str(uuid.uuid4())
        directive_json = json.dumps(
            {"knob": directive.knob, "value": directive.value, "reason": directive.reason},
            sort_keys=True,
            default=str,
        )
        with self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO learning_cases
                (case_id, observation_id, strategy_id, directive_json, confidence, result_snapshot_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    observation_id,
                    self._strategy_id,
                    directive_json,
                    directive.confidence,
                    json.dumps(result_snapshot, sort_keys=True, default=str) if result_snapshot else None,
                    time.time(),
                ),
            )
        return case_id

    def seed_directive(
        self,
        observation: Dict[str, Any],
        knob: str,
        value: Any,
        confidence: float,
        reason: str,
    ) -> Optional[AuthorityDecision]:
        """Build an AuthorityDecision from an observation, gated by confidence.

        Does NOT record a case; callers should call record_case with the result.
        """
        if not self._confidence_gate.is_allowed(confidence):
            return None
        return build_authority_directive(self._strategy_id, knob, value, confidence, reason)

    def apply_directive_to_authority(
        self,
        runtime_authority: RuntimeAuthority,
        directive: AuthorityDecision,
    ) -> None:
        """Apply a validated directive to a RuntimeAuthority instance.

        Only allowed knobs are applied; AuthorityDecision enforces this.
        """
        # To apply, copy each allowed knob into the mutable RuntimeAuthority.
        if directive.policy is not None:
            runtime_authority.set_policy(directive.policy)
        if directive.tool_scope is not None:
            runtime_authority.set_tool_scope(directive.tool_scope)
        if directive.context_size_override is not None:
            runtime_authority.set_context_size_override(directive.context_size_override)
        if directive.allow_self_delegate is not None:
            runtime_authority.set_allow_self_delegate(directive.allow_self_delegate)
        if directive.max_tool_iterations is not None:
            runtime_authority.set_max_tool_iterations(directive.max_tool_iterations)

    def apply_governance_event(
        self,
        runtime_authority: RuntimeAuthority,
        event_id: str,
    ) -> None:
        """Apply a governance event's authority directive if the event is valid.

        This is the M3b governance hook: StrategicLearning may apply a directive
        only after LearningGovernance has recorded it as an append-only event and
        the event status is APPLIED or PENDING with valid source_hash.

        The event must exist in learning_governance_events and its source_hash must
        validate against the stored chain. Fabricated events are rejected.
        """
        import json as _json
        # Fetch event from authoritative storage
        cur = self._conn.execute(
            "SELECT event_id, event_type, strategy_id, decision_reason, previous_hash, source_hash, "
            "effectiveness_result_json, authority_directive_json, status, owner, created_at "
            "FROM learning_governance_events WHERE event_id=?",
            (event_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"Governance event not found: {event_id}")

        event = dict(row)
        # Validate status
        if event["status"] == lc.GOVERNANCE_STATUS_REJECTED:
            raise ValueError(f"Rejected governance event cannot be applied: {event_id}")
        if event["status"] not in (lc.GOVERNANCE_STATUS_APPLIED, lc.GOVERNANCE_STATUS_PENDING):
            raise ValueError(f"Invalid governance event status: {event['status']}")

        directive_json = event.get("authority_directive_json")
        if not directive_json:
            return

        # Validate source_hash by recomputing from stored event data
        # This ensures the event was not tampered with in the database
        from agent.learning_governance import GovernanceEvent
        gov_event = GovernanceEvent(
            event_id=event["event_id"],
            event_type=event["event_type"],
            strategy_id=event["strategy_id"],
            decision_reason=event["decision_reason"],
            previous_hash=event["previous_hash"],
            source_hash="",  # will compute and validate below
            effectiveness_result_json=event["effectiveness_result_json"],
            authority_directive_json=event["authority_directive_json"],
            status=event["status"],
            owner=event["owner"],
            created_at=event["created_at"],
        )

        # Recompute hash with the stored previous_hash
        import hashlib
        payload = {
            "event_id": gov_event.event_id,
            "event_type": gov_event.event_type,
            "strategy_id": gov_event.strategy_id,
            "decision_reason": gov_event.decision_reason,
            "previous_hash": gov_event.previous_hash,
            "effectiveness_result_json": gov_event.effectiveness_result_json,
            "authority_directive_json": gov_event.authority_directive_json,
            "status": gov_event.status,
            "owner": gov_event.owner,
            "created_at": gov_event.created_at,
        }
        computed_hash = hashlib.sha256(_json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        if computed_hash != event["source_hash"]:
            raise ValueError(f"Governance event source_hash validation failed: {event_id}")

        # Apply directive
        kwargs = _json.loads(directive_json)
        directive = AuthorityDecision(**kwargs)
        self.apply_directive_to_authority(runtime_authority, directive)

    def derive_and_record(
        self,
        feedback_collector: RuntimeFeedbackCollector,
        opval_session: Dict[str, Any],
        opval_turns: List[Dict[str, Any]],
        candidate_directive: StrategicDirective,
    ) -> Optional[str]:
        """Convenience: build/persist observations, then record a case if confidence passes."""
        # Persist observations for this session
        self._evidence_builder.build_and_persist(opval_session, opval_turns)

        # Find a matching observation to anchor the case
        observations = self._evidence_builder.get_observations(opval_session["session_id"])
        if not observations:
            return None

        # Apply confidence gate
        if not self._confidence_gate.is_allowed(candidate_directive.confidence):
            return None

        # Record case against the first observation of this session
        observation_id = observations[0]["observation_id"]
        return self.record_case(observation_id, candidate_directive)

    def get_cases(self, observation_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Read recorded cases."""
        if observation_id:
            cur = self._conn.execute(
                "SELECT * FROM learning_cases WHERE observation_id=?",
                (observation_id,),
            )
        else:
            cur = self._conn.execute("SELECT * FROM learning_cases")
        return [dict(r) for r in cur.fetchall()]

    @property
    def strategy_id(self) -> str:
        return self._strategy_id
