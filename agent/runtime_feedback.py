"""Runtime Feedback for Learning Governance V1."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agent import learning_constants as lc
from agent.runtime_authority import AuthorityDecision, RuntimeAuthority, apply_decision, hash_decision

__all__ = ["RuntimeFeedbackCollector", "ensure_runtime_feedback"]

logger = logging.getLogger(__name__)


@dataclass
class RuntimeFeedbackCollector:
    """Collects per-turn outcomes and produces bounded authority adaptations.

    Milestone 1: records outcomes and applies simple adaptations only to
    allowed authority knobs. No database writes.

    Evidence ownership: this module owns all feedback events it produces.
    """

    events: List[Dict[str, Any]] = field(default_factory=list)
    last_decision_hash: Optional[str] = None
    source_module: str = "agent.runtime_feedback"
    owner: str = "learning-governance-v1"

    def record_tool_outcome(self, outcome: Dict[str, Any]) -> None:
        """Record a tool execution outcome."""
        self._validate_outcome(outcome)
        if len(self.events) >= lc.MAX_FEEDBACK_EVENTS_PER_TURN:
            logger.debug("Feedback event limit reached; dropping tool outcome")
            return
        self.events.append({"event_type": "tool", **dict(outcome)})
        logger.debug("Recorded tool outcome: %s", outcome)

    def record_delegate_outcome(self, outcome: Dict[str, Any]) -> None:
        """Record a delegate task outcome."""
        self._validate_outcome(outcome)
        if "task_count" not in outcome:
            raise ValueError("delegate outcome must include task_count")
        if len(self.events) >= lc.MAX_FEEDBACK_EVENTS_PER_TURN:
            logger.debug("Feedback event limit reached; dropping delegate outcome")
            return
        self.events.append({"event_type": "delegate", **dict(outcome)})
        logger.debug("Recorded delegate outcome: %s", outcome)

    def record_compression_outcome(self, outcome: Dict[str, Any]) -> None:
        """Record a compression outcome (Milestone 1 stub)."""
        self._validate_outcome(outcome)
        if len(self.events) >= lc.MAX_FEEDBACK_EVENTS_PER_TURN:
            logger.debug("Feedback event limit reached; dropping compression outcome")
            return
        self.events.append({"event_type": "compression", **dict(outcome)})

    def record_recovery_outcome(self, outcome: Dict[str, Any]) -> None:
        """Record a recovery outcome (Milestone 1 stub)."""
        self._validate_outcome(outcome)
        if len(self.events) >= lc.MAX_FEEDBACK_EVENTS_PER_TURN:
            logger.debug("Feedback event limit reached; dropping recovery outcome")
            return
        self.events.append({"event_type": "recovery", **dict(outcome)})

    def _validate_outcome(self, outcome: Dict[str, Any]) -> None:
        if not isinstance(outcome, dict):
            raise ValueError("outcome must be a dict")
        if "tool_name" not in outcome:
            raise ValueError("outcome must include tool_name")
        if "outcome" not in outcome:
            raise ValueError("outcome must include outcome field")
        if outcome["outcome"] not in {
            lc.FEEDBACK_OUTCOME_SUCCESS,
            lc.FEEDBACK_OUTCOME_FAILURE,
            lc.FEEDBACK_OUTCOME_DEFERRED,
        }:
            raise ValueError(f"invalid outcome value: {outcome['outcome']}")

    def adapt(
        self,
        runtime_authority: RuntimeAuthority,
    ) -> Optional[AuthorityDecision]:
        """Compute a bounded authority decision from feedback.

        Only allowed authority knobs may be affected. Milestone 1 implements
        a minimal, conservative policy.
        """
        updates: Dict[str, Any] = {}

        tool_events = [e for e in self.events if e.get("event_type") == "tool"]
        total_tools = len(tool_events)
        if total_tools == 0:
            return None

        failures = sum(1 for e in tool_events if e.get("outcome") == lc.FEEDBACK_OUTCOME_FAILURE)
        failure_rate = failures / total_tools

        # Conservative thresholds from Milestone 1 plan.
        if failure_rate >= 0.5 and total_tools >= 2:
            updates["policy"] = lc.AUTHORITY_POLICY_STRICT
            updates["allow_self_delegate"] = False
        elif failure_rate >= 0.3 and total_tools >= 3:
            updates["policy"] = lc.AUTHORITY_POLICY_STRICT

        if not updates:
            return None

        decision = AuthorityDecision(**updates)
        new_hash = hash_decision(decision)
        if self.last_decision_hash == new_hash:
            return None  # idempotent: already applied this decision
        self.last_decision_hash = new_hash
        return decision

    def clear(self) -> None:
        self.events.clear()


def ensure_runtime_feedback(agent: Any) -> "RuntimeFeedbackCollector":
    """Initialize a fresh per-turn feedback collector on the agent.

    Per-turn semantics (Milestone 1): each turn observes only its own tool
    outcomes. A fresh collector means a heavy-failure turn can tighten policy
    for the rest of that turn (via :meth:`RuntimeFeedbackCollector.adapt`),
    after which the next turn starts from a clean slate. No persistence and no
    DB writes. Exception-safe by construction: it only assigns an attribute.
    """
    collector = RuntimeFeedbackCollector()
    agent._runtime_feedback = collector
    return collector
