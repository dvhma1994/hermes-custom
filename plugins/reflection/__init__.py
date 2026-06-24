"""reflection — Reflexion-style cross-turn self-reflection plugin for Hermes.

Hermes fires the ``pre_llm_call`` plugin hook **once per turn** (in
``build_turn_context``, before the tool-iteration loop), so a plugin cannot
inject *mid*-turn after an individual tool fails. What it CAN do well — and what
this plugin does — is **cross-turn reflection**: at the start of a new turn, if
the *previous* turn contained tool/verification failures, inject a short
"root cause + different approach" nudge so the agent doesn't repeat the mistake.
This is complementary to (not redundant with) the within-turn oscillation guard
(``HERMES_OSC_GUARD``), which handles repeated failures inside a single turn.

The nudge is delivered through the ``pre_llm_call`` hook's ``{"context": ...}``
return value, which Hermes appends to the current turn's *user* message — never
the system prompt — so the prompt-cache prefix is preserved (no invalidation).

Safety / design:
- **Flag-gated**: no-op unless ``HERMES_REFLECTION=1``. Even when the plugin is
  loaded (``plugins.enabled``), the hook returns ``None`` immediately when the
  flag is unset, so the default path is byte-identical.
- **Fail-open**: every code path is wrapped; any error returns ``None`` so the
  agent loop is never disrupted.
- **Bounded**: injects at most once per (session, turn), and only when a real
  failure signature is found in the trailing tool-result batch. A small LRU-ish
  set caps memory.
- **No hot-path surgery**: uses only the public plugin hook surface.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# --- tuning constants --------------------------------------------------------
_MAX_FAILURES = 4       # cap how many failures we summarize in the nudge
_ERR_CLIP = 180         # max chars per error summary
_SEEN_CAP = 1000        # cap the dedup set before clearing

# Tracks (session_id, turn_id) pairs we've already nudged so we never inject
# twice for the same turn. Process-local; cleared when it grows too large.
_seen_turns: set = set()

# Failure signatures. Primary: a tool returning {"success": false, ...}
# (the canonical Hermes tool_error shape). Secondary: an explicit traceback.
_SUCCESS_FALSE_RE = re.compile(r'"success"\s*:\s*false', re.IGNORECASE)
_TRACEBACK_RE = re.compile(r"traceback \(most recent call last\)", re.IGNORECASE)


def _flag_on() -> bool:
    """True only when HERMES_REFLECTION is set to a truthy value."""
    try:
        from utils import env_var_enabled

        return env_var_enabled("HERMES_REFLECTION")
    except Exception:
        return False


def _content_text(content: Any) -> str:
    """Best-effort flatten of a message ``content`` field to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for blk in content:
            if isinstance(blk, dict):
                parts.append(str(blk.get("text") or blk.get("content") or ""))
            else:
                parts.append(str(blk))
        return " ".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def _looks_failed(text: str) -> bool:
    """Detect a failed tool result from its serialized content."""
    if not text:
        return False
    return bool(_SUCCESS_FALSE_RE.search(text) or _TRACEBACK_RE.search(text))


def _summarize_failure(text: str) -> str:
    """Extract a short human summary of the failure from a tool result."""
    # Prefer the structured "error" field when the content is JSON.
    snippet = ""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                snippet = str(obj.get("error") or obj.get("message") or "")
        except Exception:
            snippet = ""
    if not snippet:
        # Fall back to the first traceback/last line of free-form text.
        line = stripped.splitlines()[0] if stripped else ""
        snippet = line
    snippet = " ".join(snippet.split())  # collapse whitespace
    if len(snippet) > _ERR_CLIP:
        snippet = snippet[: _ERR_CLIP - 1].rstrip() + "…"
    return snippet or "(no error message)"


def _previous_turn_failures(history: List[Any]) -> List[str]:
    """Return failure summaries from the *previous* turn's tool results.

    A turn's span is delimited by ``user`` messages. The last user message is
    the current turn; the span between the second-to-last and last user message
    is the previous turn. We collect tool/function failures in that span (so we
    reflect on what failed last turn, at the start of this one). Failures inside
    the *current* turn happen after the last user message and are intentionally
    excluded — the per-turn hook cadence cannot re-inject mid-turn anyway.
    """
    if not isinstance(history, list) or not history:
        return []
    user_idxs = [
        i for i, m in enumerate(history)
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    if not user_idxs:
        return []
    last_user = user_idxs[-1]
    prev_user = user_idxs[-2] if len(user_idxs) >= 2 else -1
    span = history[prev_user + 1:last_user]
    fails: List[str] = []
    for msg in span:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") in ("tool", "function"):
            text = _content_text(msg.get("content"))
            if _looks_failed(text):
                fails.append(_summarize_failure(text))
    return fails[:_MAX_FAILURES]


def _build_nudge(failures: List[str]) -> str:
    """Compose the reflection nudge text from detected failures."""
    bullets = "\n".join(f"  - {f}" for f in failures)
    return (
        "⚠️ Reflexion check — in your previous turn, these tool call(s) failed:\n"
        f"{bullets}\n"
        "Before proceeding, pause and reflect briefly:\n"
        "1) What is the ROOT CAUSE of the failure (not just the symptom)?\n"
        "2) What DIFFERENT approach or corrected arguments will actually fix it?\n"
        "Do not repeat the same failing call with the same arguments. If an "
        "attempt already failed this way, change strategy or ask for the missing "
        "input rather than looping."
    )


def reflection_hook(**kwargs: Any) -> Optional[Dict[str, str]]:
    """``pre_llm_call`` callback. Returns ``{"context": nudge}`` or ``None``.

    Fail-open: any unexpected error results in ``None`` (no injection).
    """
    try:
        if not _flag_on():
            return None
        history = kwargs.get("conversation_history") or []
        failures = _previous_turn_failures(history)
        if not failures:
            return None

        session_id = kwargs.get("session_id")
        turn_id = kwargs.get("turn_id")
        key = (session_id, turn_id)
        # Only dedup when we have a usable turn key; otherwise allow (rare).
        if turn_id is not None:
            if key in _seen_turns:
                return None
            if len(_seen_turns) >= _SEEN_CAP:
                _seen_turns.clear()
            _seen_turns.add(key)

        nudge = _build_nudge(failures)
        logger.debug("reflection: injecting nudge for %d failure(s)", len(failures))
        return {"context": nudge}
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("reflection hook failed (ignored): %s", exc)
        return None


def register(ctx: Any) -> None:
    """Plugin entry point — register the pre_llm_call hook."""
    ctx.register_hook("pre_llm_call", reflection_hook)
    logger.debug("reflection plugin registered (HERMES_REFLECTION gates activity)")
