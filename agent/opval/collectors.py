"""Evidence collectors for OPVAL sessions and turns.

Hardened for LGU-01:
- Drift detection no longer suppresses drift_flag on scope_change.
- SessionCollector computes tool_execution_count and root_session_id.
- session_tool_correctness is kept for diagnostics only.
"""

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from .store import OpvalStore
from .evaluators import IntentExtractor, MisalignmentValidator, OutcomeClassifier


class TurnCollector:
    """Collect and score a single turn."""

    def __init__(self, store: OpvalStore, session_id: str):
        self._store = store
        self._session_id = session_id

    def collect(
        self,
        turn_number: int,
        user_message: str,
        agent_message: str,
        tool_calls: List[str],
        tool_outputs: List[Dict[str, Any]],
        error: Optional[Exception] = None,
        interrupted: bool = False,
        clarify: bool = False,
        latency_ms: int = 0,
        tokens_used: int = 0,
        checkpoint_event: Optional[str] = None,
        anchor: Optional[Dict[str, Any]] = None,
        prior_error_classes: Optional[List[str]] = None,
        started_at: Optional[float] = None,
        ended_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        # Normalize tool_outputs so callers can pass malformed data without breaking storage
        normalized_tool_outputs: List[Dict[str, Any]] = []
        if isinstance(tool_outputs, list):
            for out in tool_outputs:
                if isinstance(out, dict):
                    normalized_tool_outputs.append(out)
                else:
                    normalized_tool_outputs.append({"exit_code": 0, "error": None, "raw": str(out)[:500]})
        elif tool_outputs is not None:
            normalized_tool_outputs.append({"exit_code": 0, "error": None, "raw": str(tool_outputs)[:500]})

        outcome = OutcomeClassifier.classify(
            error=error,
            interrupted=interrupted,
            clarify=clarify,
            tool_outputs=normalized_tool_outputs,
        )

        # Tool execution correctness: average of (exit_code == 0) over all tool outputs
        tool_execution_scores = []
        for out in normalized_tool_outputs:
            if isinstance(out, dict) and out.get("error") is not None:
                tool_execution_scores.append(0.0)
            elif isinstance(out, dict) and out.get("exit_code") not in (None, 0):
                tool_execution_scores.append(0.0)
            else:
                tool_execution_scores.append(1.0)
        tool_execution_score = sum(tool_execution_scores) / max(len(tool_execution_scores), 1)

        record = {
            "turn_id": f"{self._session_id}:{turn_number}:{uuid.uuid4().hex[:8]}",
            "session_id": self._session_id,
            "turn_number": turn_number,
            "outcome": outcome,
            "tool_calls": json.dumps(tool_calls),
            "tool_outputs": json.dumps(normalized_tool_outputs),
            "error_class": type(error).__name__ if error is not None else None,
            "latency_ms": latency_ms,
            "tokens_used": tokens_used,
            "tool_execution_score": tool_execution_score,
            "started_at": started_at or time.time(),
            "ended_at": ended_at or time.time(),
            "checkpoint_event": checkpoint_event,
        }

        turn_for_validator = {
            "user_message": user_message,
            "agent_message": agent_message,
            "tool_calls": tool_calls,
            "tool_outputs": normalized_tool_outputs,
            "outcome": outcome,
            "error_class": record["error_class"],
        }

        drift_flag = 0
        if anchor and turn_number > 1:
            semantic, artifact = IntentExtractor.compute_intent_similarity(
                anchor.get("anchor_message", ""), user_message, tool_calls
            )
            intent_score = 0.6 * semantic + 0.4 * artifact
            constraint_score = 0 if MisalignmentValidator._constraint_violation(
                anchor.get("constraints", []), tool_calls
            ) else 1
            lineage_score = 1 if (
                IntentExtractor.detect_continuation(user_message)
                or IntentExtractor.detect_scope_change(user_message)
            ) else 0
            drift_score = (
                0.40 * intent_score
                + 0.35 * constraint_score
                + 0.20 * lineage_score
                + 0.05 * (1 if IntentExtractor.detect_scope_change(user_message) else 0)
            )
            if drift_score < 0.50 or constraint_score == 0:
                drift_flag = 1

        misaligned, _ = MisalignmentValidator.validate_turn(
            turn_for_validator,
            anchor or {},
            prior_error_classes or [],
        )
        record["drift_flag"] = drift_flag
        record["misalignment_flag"] = 1 if misaligned else 0

        # Simple quality score: success=1, clarify=0.7, failure/error=0, interrupt=0
        quality_map = {"success": 1.0, "clarify": 0.7, "failure": 0.0, "error": 0.0, "interrupt": 0.0}
        record["quality_score"] = quality_map.get(outcome, 0.0)

        self._store.insert_turn(record)
        return record


class SessionCollector:
    """Collect and finalize a session record."""

    def __init__(self, store: OpvalStore):
        self._store = store

    def finalize(
        self,
        session_id: str,
        task_id: str,
        platform: str,
        start_time: float,
        end_time: float,
        anchor_message: str,
        turns: List[Dict[str, Any]],
        parent_session_id: Optional[str] = None,
        synthetic: bool = False,
        synthetic_reason: Optional[str] = None,
        state_db_conn: Optional[Any] = None,
    ) -> Dict[str, Any]:
        # Domain classification from all turn tool activity, not just the anchor message.
        all_tool_calls: List[str] = []
        all_user_messages = [anchor_message]
        for t in turns:
            if not isinstance(t, dict):
                continue
            try:
                tc = json.loads(t.get("tool_calls", "[]")) if isinstance(t.get("tool_calls"), str) else t.get("tool_calls", [])
                if isinstance(tc, list):
                    all_tool_calls.extend(tc)
            except (json.JSONDecodeError, TypeError):
                pass
            um = t.get("user_message") or t.get("original_user_message")
            if um:
                all_user_messages.append(str(um))
        combined_text = " ".join(all_user_messages)
        primary, secondary = IntentExtractor.classify_domain(
            combined_text,
            all_tool_calls,
        )

        turn_count = len(turns)
        total = max(turn_count, 1)
        success_turns = sum(1 for t in turns if isinstance(t, dict) and t.get("outcome") == "success")
        drifted = sum(t.get("drift_flag", 0) for t in turns if isinstance(t, dict))
        misaligned = sum(t.get("misalignment_flag", 0) for t in turns if isinstance(t, dict))
        quality_scores = [t.get("quality_score", 0.0) for t in turns if isinstance(t, dict)]
        avg_quality = sum(quality_scores) / total if quality_scores else 0.0

        tool_exec_scores = [
            t.get("tool_execution_score", 0.0)
            for t in turns
            if isinstance(t, dict) and t.get("tool_execution_score") is not None
        ]
        session_tool_correctness = (
            sum(tool_exec_scores) / len(tool_exec_scores) if tool_exec_scores else 0.0
        )

        # tool_execution_count = number of tool outputs with exit_code == 0
        tool_execution_count = 0
        for t in turns:
            if not isinstance(t, dict):
                continue
            try:
                outs = json.loads(t.get("tool_outputs", "[]")) if isinstance(t.get("tool_outputs"), str) else t.get("tool_outputs", [])
                if isinstance(outs, list):
                    for out in outs:
                        if isinstance(out, dict) and out.get("error") is None and out.get("exit_code") in (0, None):
                            tool_execution_count += 1
            except (json.JSONDecodeError, TypeError):
                pass

        # Outcome: success if final turn is success and avg_quality >= 0.5
        final_outcome = turns[-1]["outcome"] if turns and isinstance(turns[-1], dict) else "error"
        outcome = "success" if final_outcome == "success" and avg_quality >= 0.5 else final_outcome

        # Compute root_session_id from parent chain, with state.db fallback
        root_session_id = self._store.resolve_root_session_id(session_id, state_db_conn)

        record = {
            "session_id": session_id,
            "task_id": task_id,
            "parent_session_id": parent_session_id,
            "root_session_id": root_session_id,
            "platform": platform,
            "primary_domain": primary,
            "secondary_domains": json.dumps(secondary),
            "start_time": start_time,
            "end_time": end_time,
            "turn_count": turn_count,
            "tool_execution_count": tool_execution_count,
            "outcome": outcome,
            "session_quality_score": avg_quality,
            "session_tool_correctness": session_tool_correctness,
            "drift_pct": (drifted / total) * 100,
            "misalignment_pct": (misaligned / total) * 100,
            "promotion_score": 0.0,
            "synthetic": 1 if synthetic else 0,
            "synthetic_reason": synthetic_reason,
            "opval_enabled": 1 if os.getenv("HERMES_OPVAL") == "1" else 0,
            "recorded_at": time.time(),
        }
        self._store.insert_session(record)
        return record
