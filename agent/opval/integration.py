"""Integration hooks for OPVAL evidence collection.

This module bridges the agent runtime with the OPVAL store. It is only active
when HERMES_OPVAL=1. It does not activate Strategic Learning or modify
Architecture Freeze v1 behavior.
"""

import os
import time
import uuid
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

from .store import OpvalStore
from .collectors import TurnCollector, SessionCollector
from .evaluators import IntentExtractor


class OpvalSessionRecorder:
    """Records a single session's turns and finalizes the session artifact."""

    def __init__(self, store: OpvalStore, session_id: str, task_id: str, platform: str, anchor_message: str, synthetic: bool = False, synthetic_reason: Optional[str] = None):
        self._store = store
        self._session_id = session_id
        self._task_id = task_id
        self._platform = platform
        self._anchor_message = anchor_message
        self._synthetic = synthetic
        self._synthetic_reason = synthetic_reason
        self._start_time = time.time()
        # Capture the original session_id used when this recorder was created.
        # During compression/rotation the caller may update _session_id before
        # finalize(); the original value lets us clean up the placeholder row.
        self._original_session_id = session_id
        self._turn_collector = TurnCollector(store, session_id)
        self._turn_number = 0
        self._anchor = {
            "anchor_message": anchor_message,
            **IntentExtractor.extract_anchor(anchor_message),
        }
        self._prior_error_classes: List[str] = []
        self._turn_records: List[Dict[str, Any]] = []
        # Insert a placeholder session row immediately so that per-turn rows
        # satisfy the opval_turns foreign-key constraint. The row is replaced
        # when finalize() is called.
        try:
            self._store.insert_session({
                "session_id": session_id,
                "task_id": task_id,
                "platform": platform,
                "primary_domain": self._anchor.get("primary_domain", ""),
                "outcome": "incomplete",
                "parent_session_id": None,
                "synthetic": int(synthetic),
                "synthetic_reason": synthetic_reason,
                "recorded_at": self._start_time,
            })
        except Exception:
            # If the row already exists (e.g., resumed/compressed session) the
            # foreign-key relationship still holds; finalize() will update it.
            pass

    def record_turn(
        self,
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
    ) -> None:
        """Callback shape expected from conversation_loop.build_turn_context."""
        self._turn_number += 1
        record = self._turn_collector.collect(
            turn_number=self._turn_number,
            user_message=user_message,
            agent_message=agent_message,
            tool_calls=tool_calls,
            tool_outputs=tool_outputs,
            error=error,
            interrupted=interrupted,
            clarify=clarify,
            latency_ms=latency_ms,
            tokens_used=tokens_used,
            checkpoint_event=checkpoint_event,
            anchor=self._anchor,
            prior_error_classes=list(self._prior_error_classes),
            started_at=self._start_time,
            ended_at=time.time(),
        )
        self._turn_records.append(record)
        ec = record.get("error_class")
        if ec:
            self._prior_error_classes.append(ec)

    def finalize(
        self,
        outcome: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        session_lineage: Optional[str] = None,
        session_id: Optional[str] = None,
        synthetic: bool = False,
        state_db_conn: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """Finalize the session record.

        Args:
            outcome: Override outcome.
            parent_session_id: Immediate parent session_id.
            session_lineage: Pre-computed root-to-tip lineage string. (deprecated)
            session_id: Updated session_id if compression rotated during the turn.
            synthetic: Mark as synthetic (e.g., gateway resume_pending turn).
            state_db_conn: Optional state.db connection for root_session_id validation.
        """
        if not self._turn_records:
            return None
        # Allow caller to update session_id after compression/rotation.
        if session_id:
            self._session_id = session_id
        session = SessionCollector(self._store).finalize(
            session_id=self._session_id,
            task_id=self._task_id,
            platform=self._platform,
            start_time=self._start_time,
            end_time=time.time(),
            anchor_message=self._anchor_message,
            turns=self._turn_records,
            parent_session_id=parent_session_id,
            synthetic=synthetic or self._synthetic,
            synthetic_reason=self._synthetic_reason,
            state_db_conn=state_db_conn,
        )

        if outcome:
            session["outcome"] = outcome
            self._store.insert_session(session)
        # AUD-01 cleanup: when compression/rotation changed the session_id, the
        # placeholder row inserted at __init__ time becomes an orphan. Move the
        # captured turn(s) to the final session_id and delete the placeholder row.
        # Failure isolation: any error here is swallowed; the system degrades to
        # the pre-fix state (orphan placeholder) and readiness.py defensively
        # handles NULLs.
        old_session_id = getattr(self, "_original_session_id", None)
        if old_session_id and session_id and old_session_id != session_id:
            try:
                self._store._conn.execute(
                    "UPDATE opval_turns SET session_id=? WHERE session_id=?",
                    (session_id, old_session_id),
                )
                self._store._conn.execute(
                    "DELETE FROM opval_sessions WHERE session_id=? AND outcome=?",
                    (old_session_id, "incomplete"),
                )
                self._store._conn.commit()
            except Exception as e:
                logger.warning("OPVAL placeholder cleanup failed for %s -> %s: %s", old_session_id, session_id, e)
        return session

    def _build_full_lineage(self, parent_session_id: Optional[str] = None) -> str:
        """Build a root-to-tip lineage string.

        If parent_session_id is provided, use it as the immediate parent.
        The authoritative source is sessions.parent_session_id via the agent's
        existing SessionDB instance.
        """
        chain: List[str] = [self._session_id]
        if parent_session_id:
            chain.insert(0, parent_session_id)
        return ">".join(chain)

    def close(self) -> None:
        """Drop the TurnCollector reference; do NOT mutate the shared store.

        The store is closed by the caller (HK03 in conversation_loop.py)
        after recorder.finalize() completes. Mutating _store to None here
        would create shared-mutable-state issues if any code retained a
        reference to the TurnCollector after close().
        """
        if self._turn_collector is not None:
            # Drop the reference; do not mutate the store attribute.
            self._turn_collector = None  # type: ignore[assignment]

    @property
    def session_lineage(self) -> Optional[str]:
        return self._session_id


def opval_enabled() -> bool:
    return os.getenv("HERMES_OPVAL") == "1"


def default_store_path() -> str:
    # Resolve default Hermes state.db from the local app data directory.
    local_app_data = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
    return os.path.join(local_app_data, "hermes", "state.db")
