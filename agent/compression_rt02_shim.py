"""
RT-02 integration shim for agent/conversation_compression.py.

This module provides a single entry point that uses CompressionCoordinator
for the lock/lineage/rotation/idempotency protocol while preserving all
existing compression strategy logic in ConversationCompressor.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _coordinator(agent: Any) -> Any:
    """Return (or create) the agent's CompressionCoordinator."""
    coord = getattr(agent, "_compression_coordinator", None)
    if coord is None:
        from agent.compression_coordinator import CompressionCoordinator

        db = getattr(agent, "_session_db", None)
        config = getattr(agent, "config", {})
        coord = CompressionCoordinator(db, config)
        agent._compression_coordinator = coord
    return coord


def compress_context_rt02(
    agent: Any,
    messages: List[dict],
    system_message: str,
    *,
    approx_tokens: Optional[int] = None,
    task_id: str = "default",
    focus_topic: Optional[str] = None,
    force: bool = False,
) -> Tuple[List[dict], str]:
    """
    RT-02-aware compression entry point.

    This is the intended replacement for ``compress_context`` in the
    long term.  For now, it is used as the atomic rotation layer after
    the legacy :class:`ConversationCompressor` has produced a compressed
    message list.

    Steps:
    1. Run legacy compression (summarization strategies, etc.) unchanged.
    2. If compression actually reduced messages, route the rotation through
       :class:`CompressionCoordinator` so that lock/lineage/rollback
       semantics are enforced.
    3. Fall back to the legacy session-split code if the coordinator is
       unavailable or if its database methods are not present.
    """
    # Lazy feasibility check (same as legacy entry)
    from agent.conversation_compression import (
        COMPACTION_STATUS,
        check_compression_model_feasibility,
    )

    if not getattr(agent, "_compression_feasibility_checked", False):
        check_compression_model_feasibility(agent)
        agent._compression_feasibility_checked = True

    _pre_msg_count = len(messages)
    logger.info(
        "context compression started (RT-02): session=%s messages=%d tokens=~%s model=%s focus=%r",
        agent.session_id or "none",
        _pre_msg_count,
        f"{approx_tokens:,}" if approx_tokens else "unknown",
        agent.model,
        focus_topic,
    )
    agent._emit_status(COMPACTION_STATUS)

    # Notify external memory provider before compression discards context
    if agent._memory_manager:
        try:
            agent._memory_manager.on_pre_compress(messages)
        except Exception:
            pass

    # Run the legacy compressor for strategy/summarization.
    try:
        compressed = agent.context_compressor.compress(
            messages, current_tokens=approx_tokens, focus_topic=focus_topic, force=force
        )
    except TypeError:
        compressed = agent.context_compressor.compress(messages, current_tokens=approx_tokens)

    # If compression aborted, return unchanged.
    if getattr(agent.context_compressor, "_last_compress_aborted", False):
        _err = (
            getattr(agent.context_compressor, "_last_summary_error", None)
            or "unknown error"
        )
        if getattr(agent, "_last_compression_summary_warning", None) != _err:
            agent._last_compression_summary_warning = _err
            agent._emit_warning(
                f"⚠ Compression aborted: {_err}. "
                "No messages were dropped — conversation continues unchanged. "
                "Run /compress to retry, or /new to start a fresh session."
            )
        _existing_sp = getattr(agent, "_cached_system_prompt", None)
        if not _existing_sp:
            _existing_sp = agent._build_system_prompt(system_message)
        return messages, _existing_sp

    # No-op (LLM produced same number of messages)
    if len(compressed) >= _pre_msg_count:
        _existing_sp = getattr(agent, "_cached_system_prompt", None)
        if not _existing_sp:
            _existing_sp = agent._build_system_prompt(system_message)
        return compressed, _existing_sp

    # Build new system prompt
    todo_snapshot = agent._todo_store.format_for_injection()
    if todo_snapshot:
        compressed.append({"role": "user", "content": todo_snapshot})

    agent._invalidate_system_prompt()
    new_system_prompt = agent._build_system_prompt(system_message)
    agent._cached_system_prompt = new_system_prompt

    # Route rotation through RT-02 coordinator
    db = getattr(agent, "_session_db", None)
    old_session_id = agent.session_id
    trigger = "MANUAL_FORCE" if force else "AUTO_THRESHOLD"
    strategy = getattr(agent.context_compressor, "last_strategy", "summarize_old") or "summarize_old"

    if db is not None:
        try:
            coord = _coordinator(agent)
            result = coord.coordinate_compress(
                session_id=old_session_id,
                trigger=trigger,
                messages=messages,
                strategy=strategy,
                agent_state={
                    "system_prompt": system_message,
                    "model": agent.model,
                    "platform": agent.platform,
                },
                force=force,
                mode="INTERACTIVE",
                provider=getattr(agent, "provider", None),
                model=agent.model,
            )
            if result.success and result.child_session_id:
                agent.session_id = result.child_session_id
                _set_session_context(agent.session_id)
                agent._session_db_created = True
                agent._session_db.update_system_prompt(agent.session_id, new_system_prompt)
                agent._last_flushed_db_idx = 0

                # Notify context engine
                try:
                    if hasattr(agent.context_compressor, "on_session_start"):
                        agent.context_compressor.on_session_start(
                            agent.session_id or "",
                            boundary_reason="compression",
                            old_session_id=old_session_id,
                            conversation_id=getattr(agent, "_gateway_session_key", None),
                        )
                except Exception as _ce_err:
                    logger.debug("context engine on_session_start (compression): %s", _ce_err)

                # Notify memory manager
                try:
                    if agent._memory_manager:
                        agent._memory_manager.on_session_switch(
                            agent.session_id or "",
                            parent_session_id=old_session_id,
                            reset=False,
                            reason="compression",
                        )
                except Exception as _me_err:
                    logger.debug("memory manager on_session_switch (compression): %s", _me_err)

                # Event callback
                if getattr(agent, "event_callback", None):
                    try:
                        agent.event_callback("session:compress", {
                            "platform": agent.platform or "",
                            "session_id": agent.session_id,
                            "old_session_id": old_session_id or "",
                            "compression_count": agent.context_compressor.compression_count,
                        })
                    except Exception as e:
                        logger.debug("event_callback error on session:compress: %s", e)

                _compressed_est = estimate_request_tokens_rough(
                    compressed,
                    system_prompt=new_system_prompt or "",
                    tools=getattr(agent, 'tools', None),
                )
                agent.context_compressor.last_compression_rough_tokens = _compressed_est
                agent.context_compressor.last_prompt_tokens = -1
                agent.context_compressor.last_completion_tokens = 0
                agent.context_compressor.awaiting_real_usage_after_compression = True
                _clear_file_dedup(task_id)
                logger.info(
                    "context compression done (RT-02): session=%s messages=%d->%d "
                    "rough_tokens=~%s awaiting_real_usage=true",
                    agent.session_id or "none",
                    _pre_msg_count,
                    len(compressed),
                    f"{_compressed_est:,}",
                )
                return compressed, new_system_prompt
        except Exception as _rt02_err:
            logger.warning(
                "RT-02 coordinator rotation failed; falling back to legacy split: %s",
                _rt02_err,
            )

    # Legacy fallback when coordinator unavailable or failed.
    return _legacy_session_split(
        agent, messages, compressed, new_system_prompt, system_message, task_id
    )


def _set_session_context(session_id: str) -> None:
    """Update gateway/env/logging session context."""
    try:
        from gateway.session_context import set_current_session_id

        set_current_session_id(session_id)
    except Exception:
        os.environ["HERMES_SESSION_ID"] = session_id
    try:
        from hermes_logging import set_session_context

        set_session_context(session_id)
    except Exception:
        pass


def _clear_file_dedup(task_id: str) -> None:
    try:
        from tools.file_tools import reset_file_dedup

        reset_file_dedup(task_id)
    except Exception:
        pass


def _legacy_session_split(
    agent: Any,
    original_messages: List[dict],
    compressed: List[dict],
    new_system_prompt: str,
    system_message: str,
    task_id: str,
) -> Tuple[List[dict], str]:
    """Original session split logic, kept as a fallback."""
    from datetime import datetime
    import uuid

    if agent._session_db:
        try:
            old_title = agent._session_db.get_session_title(agent.session_id)
            agent.commit_memory_session(original_messages)
            agent._session_db.end_session(agent.session_id, "compression")
            old_session_id = agent.session_id
            agent.session_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
            _set_session_context(agent.session_id)
            agent._session_db_created = False
            agent._session_db.create_session(
                session_id=agent.session_id,
                source=agent.platform or os.environ.get("HERMES_SESSION_SOURCE", "cli"),
                model=agent.model,
                model_config=getattr(agent, '_session_init_model_config', None),
                parent_session_id=old_session_id,
            )
            agent._session_db_created = True
            if old_title:
                try:
                    new_title = agent._session_db.get_next_title_in_lineage(old_title)
                    agent._session_db.set_session_title(agent.session_id, new_title)
                except Exception as e:
                    logger.debug("Could not propagate title on compression: %s", e)
            agent._session_db.update_system_prompt(agent.session_id, new_system_prompt)
            agent._last_flushed_db_idx = 0

            try:
                if hasattr(agent.context_compressor, "on_session_start"):
                    agent.context_compressor.on_session_start(
                        agent.session_id or "",
                        boundary_reason="compression",
                        old_session_id=old_session_id,
                        conversation_id=getattr(agent, "_gateway_session_key", None),
                    )
            except Exception as _ce_err:
                logger.debug("context engine on_session_start (compression): %s", _ce_err)

            try:
                if agent._memory_manager:
                    agent._memory_manager.on_session_switch(
                        agent.session_id or "",
                        parent_session_id=old_session_id,
                        reset=False,
                        reason="compression",
                    )
            except Exception as _me_err:
                logger.debug("memory manager on_session_switch (compression): %s", _me_err)

            if getattr(agent, "event_callback", None):
                try:
                    agent.event_callback("session:compress", {
                        "platform": agent.platform or "",
                        "session_id": agent.session_id,
                        "old_session_id": old_session_id or "",
                        "compression_count": agent.context_compressor.compression_count,
                    })
                except Exception as e:
                    logger.debug("event_callback error on session:compress: %s", e)

        except Exception as e:
            logger.warning("Legacy session DB compression split failed: %s", e)

    _compressed_est = estimate_request_tokens_rough(
        compressed,
        system_prompt=new_system_prompt or "",
        tools=getattr(agent, 'tools', None),
    )
    agent.context_compressor.last_compression_rough_tokens = _compressed_est
    agent.context_compressor.last_prompt_tokens = -1
    agent.context_compressor.last_completion_tokens = 0
    agent.context_compressor.awaiting_real_usage_after_compression = True
    _clear_file_dedup(task_id)
    return compressed, new_system_prompt


# Re-export estimate helper to avoid circular imports
def estimate_request_tokens_rough(messages, system_prompt: str = "", tools=None) -> int:
    """Placeholder — real import is delegated lazily."""
    try:
        from agent.conversation_compression import estimate_request_tokens_rough as _real

        return _real(messages, system_prompt=system_prompt, tools=tools)
    except Exception:
        return 0
