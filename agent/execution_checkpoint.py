"""Persistent execution checkpoint dataclass and helpers.

This module provides the in-memory representation of an execution checkpoint
and convenience methods to convert it to/from a JSON-serializable dict for
storage in state.db.  It deliberately does NOT perform IO; callers use
``hermes_state.SessionDB`` for persistence.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ExecutionCheckpoint:
    """Snapshot of an in-flight agent task that can survive a turn boundary."""

    task_id: str
    session_id: str

    # Counters from conversation_loop.py
    api_call_count: int = 0
    iteration_budget_used: int = 0
    max_iterations: Optional[int] = None

    # Retry counter snapshots
    retry_counters: Dict[str, int] = field(default_factory=dict)

    # Snapshot of the message list at checkpoint time.
    messages_snapshot: Optional[List[Dict[str, Any]]] = None

    # Context/compression state
    compression_count: int = 0

    # Tool execution state
    pending_tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    pending_tool_arguments: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    tool_batch_order: List[str] = field(default_factory=list)
    cancelled_tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    completed_tool_call_ids: List[str] = field(default_factory=list)

    # Planning state
    todo_snapshot: Optional[str] = None

    # Metadata
    turn_exit_reason: Optional[str] = None
    task_classification: Optional[str] = None
    user_message: Optional[str] = None
    original_user_message: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None

    # Message log cursor (DB rowid of last persisted message at checkpoint time).
    # Used to avoid replaying the same transcript tail twice on resume.
    last_persisted_message_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable dictionary."""
        return asdict(self)

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionCheckpoint":
        """Reconstruct a checkpoint from a dictionary."""
        # Drop the metadata wrapper that SessionDB may have added.
        data.pop("_stored", None)
        # Only keep fields known to the dataclass; ignore stale extras.
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    @classmethod
    def from_json(cls, text: str) -> "ExecutionCheckpoint":
        """Reconstruct a checkpoint from a JSON string."""
        return cls.from_dict(json.loads(text))

    def is_expired(self) -> bool:
        """Return True if this checkpoint has passed its expiration time."""
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def touch(self) -> None:
        """Update the updated_at timestamp."""
        self.updated_at = time.time()

    def describe_resume(self) -> str:
        """Human-readable resume summary for injection into the conversation."""
        pending = len(self.pending_tool_calls)
        completed = len(self.completed_tool_call_ids)
        budget = ""
        if self.max_iterations is not None:
            remaining = max(0, self.max_iterations - self.iteration_budget_used)
            budget = f"; {remaining} iteration(s) remaining"
        return (
            f"[System: Resuming interrupted task {self.task_id}. "
            f"Already completed {self.api_call_count} model call(s) and "
            f"{completed} tool call(s). "
            f"{pending} tool call(s) pending{budget}. "
            f"Do not restart from scratch.]"
        )


def classify_task(user_message: Optional[str]) -> str:
    """Simple, deterministic task classification for resume matching.

    Uses keyword heuristics; intentionally lightweight and local.
    """
    if not user_message:
        return "general"
    text = user_message.lower()
    if any(k in text for k in ("dashboard", "ui", "layout", "component", "svg", "html", "css")):
        return "dashboard"
    if any(k in text for k in ("bug", "fix", "error", "traceback", "test fail")):
        return "debugging"
    if any(k in text for k in ("refactor", "rename", "extract", "clean up")):
        return "refactor"
    if any(k in text for k in ("write", "create", "generate", "build", "implement")):
        return "creation"
    return "general"
