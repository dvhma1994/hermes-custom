"""redact_output — defensive secret-redaction on the agent's outgoing text.

Hermes fires ``transform_llm_output`` once per turn after the tool loop, passing
``response_text``; a plugin returns a replacement string (first non-empty wins)
or ``None`` to leave the text unchanged. This plugin scans the response for
high-signal credential shapes and replaces any matches with a typed placeholder
so secrets are not leaked into a chat/gateway transcript.

Safety / design:
- **Flag-gated**: no-op unless ``HERMES_REDACT_OUTPUT=1``.
- **Fail-open**: any error → ``None`` (text passes through unchanged).
- **Precise**: strict patterns with realistic minimum lengths, so ordinary text
  and obvious placeholders (``gho_example``, ``sk-xxxx``) are not mangled.
- Returns ``None`` when nothing matched, so other ``transform_llm_output``
  plugins still get a chance to run.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# (label, compiled pattern). Order matters only for overlapping shapes; these
# are disjoint enough that order is cosmetic. Minimum lengths chosen to avoid
# matching short non-secret tokens.
_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("github-token", re.compile(r"\bgh[opusr]_[A-Za-z0-9]{16,}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("sakana-key", re.compile(r"\bfish_[a-f0-9]{32,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("gcp-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}\b")),
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
    ),
]


def _flag_on() -> bool:
    try:
        from utils import env_var_enabled

        return env_var_enabled("HERMES_REDACT_OUTPUT")
    except Exception:
        return False


def redact(text: str) -> Tuple[str, int]:
    """Return (redacted_text, num_redactions)."""
    total = 0
    out = text
    for label, pat in _PATTERNS:
        placeholder = f"‹redacted:{label}›"
        out, n = pat.subn(placeholder, out)
        total += n
    return out, total


def redact_output_hook(**kwargs) -> Optional[str]:
    """``transform_llm_output`` callback. Returns redacted text or ``None``."""
    try:
        if not _flag_on():
            return None
        text = kwargs.get("response_text")
        if not isinstance(text, str) or not text:
            return None
        redacted, n = redact(text)
        if n:
            logger.warning("redact_output: redacted %d secret(s) from outgoing text", n)
            return redacted
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("redact_output hook failed (ignored): %s", exc)
        return None


def register(ctx) -> None:
    """Plugin entry point — register the transform_llm_output hook."""
    ctx.register_hook("transform_llm_output", redact_output_hook)
    logger.debug("redact_output plugin registered (HERMES_REDACT_OUTPUT gates activity)")
