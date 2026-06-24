"""auto_index — keep the semantic-recall vector index fresh automatically.

The semantic index (``state.db.semantic.db`` sidecar) only contains what has been
backfilled; messages added after the last backfill aren't searchable until it's
re-run. This plugin re-runs an incremental, idempotent backfill on each new
session start (the ``on_session_start`` hook fires once per session — cheap, NOT
per turn), in a daemon thread so session startup is never blocked.

Safety / design:
- **Flag-gated**: no-op unless ``HERMES_AUTO_INDEX=1``.
- Also a clean no-op unless ``HERMES_SEMANTIC_RECALL=1`` and an embedder is
  reachable (``backfill_messages`` checks ``recall.available``).
- **Fail-open**: every path is wrapped; failures are swallowed.
- **Bounded**: indexes only the most recent ``_RECENT_LIMIT`` messages; backfill
  is idempotent (skips rows whose content hash is unchanged), so steady-state
  runs are cheap.
- **Non-blocking + single-flight**: runs in a daemon thread; a module-level guard
  skips spawning a second backfill while one is already in flight.
- Reads the live DB through a **separate read-only connection**, so it never
  touches the agent's own DB connection.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_RECENT_LIMIT = 400  # most-recent messages to (re)index per session start

_lock = threading.Lock()
_in_flight = False


def _flag_on() -> bool:
    try:
        from utils import env_var_enabled

        return env_var_enabled("HERMES_AUTO_INDEX")
    except Exception:
        return False


def _run_backfill(limit: int = _RECENT_LIMIT) -> int:
    """Incrementally index recent messages. Returns rows indexed (0 = no-op)."""
    global _in_flight
    try:
        from agent.semantic_recall import (
            SemanticRecall,
            backfill_messages,
            is_enabled,
        )

        if not is_enabled():
            return 0  # semantic recall disabled → nothing to maintain

        recall = SemanticRecall.for_default()
        if not getattr(recall, "available", False):
            return 0  # no embedder / no store

        import sqlite3

        from hermes_state import DEFAULT_DB_PATH

        uri = DEFAULT_DB_PATH.as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5, check_same_thread=False)
        try:
            n = backfill_messages(conn, recall, limit=limit)
        finally:
            conn.close()
        if n:
            logger.info("auto_index: refreshed %d message(s) in the semantic index", n)
        return n
    except Exception:
        logger.debug("auto_index backfill failed (ignored)", exc_info=True)
        return 0
    finally:
        with _lock:
            _in_flight = False


def _maybe_start(**kwargs) -> bool:
    """Spawn a background backfill if enabled and none is already running.

    Returns True when a thread was started, False otherwise. Public for tests.
    """
    global _in_flight
    if not _flag_on():
        return False
    with _lock:
        if _in_flight:
            return False
        _in_flight = True
    try:
        t = threading.Thread(
            target=_run_backfill,
            name="hermes-auto-index",
            daemon=True,
        )
        t.start()
        return True
    except Exception:
        with _lock:
            _in_flight = False
        return False


def session_start_hook(**kwargs) -> None:
    """``on_session_start`` callback (observer; return value ignored)."""
    try:
        _maybe_start(**kwargs)
    except Exception:  # pragma: no cover - defensive
        logger.debug("auto_index session_start_hook failed (ignored)", exc_info=True)
    return None


def register(ctx) -> None:
    """Plugin entry point — register the on_session_start hook."""
    ctx.register_hook("on_session_start", session_start_hook)
    logger.debug("auto_index plugin registered (HERMES_AUTO_INDEX gates activity)")
