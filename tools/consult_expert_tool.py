#!/usr/bin/env python3
"""consult_expert — ask a stronger expert model for genuinely hard sub-problems.

This tool is flag-gated behind ``HERMES_EXPERT_CONSULT=1`` and fail-open: it
returns JSON errors instead of raising so the calling agent can continue safely.
"""

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _extract_answer(response: Any) -> str:
    """Extract assistant text from an OpenAI-compatible chat-completions response."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", "")
    return content or ""


def consult_expert(question: str, context: str = "", db=None) -> str:
    """Consult Sakana fugu-ultra for a hard sub-problem and return JSON."""
    try:
        if not isinstance(question, str) or not question.strip():
            return json.dumps({"success": False, "error": "question required"}, ensure_ascii=False)

        prompt = question.strip()
        if isinstance(context, str) and context.strip():
            prompt = f"{prompt}\n\nContext:\n{context.strip()}"

        from agent.auxiliary_client import resolve_provider_client

        # Default to the BALANCED `fugu` (~3x faster than fugu-ultra in practice, same
        # answer on most prompts). Override with HERMES_EXPERT_MODEL=fugu-ultra only for
        # the genuinely hardest sub-problems where the extra orchestration is worth the wait.
        expert_model = (os.getenv("HERMES_EXPERT_MODEL") or "fugu").strip() or "fugu"
        res = resolve_provider_client("sakana", model=expert_model)
        client = res[0] if isinstance(res, tuple) else res
        response = client.chat.completions.create(
            model=expert_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
        )
        return json.dumps(
            {"success": True, "answer": _extract_answer(response), "model": expert_model},
            ensure_ascii=False,
        )
    except Exception as exc:
        logger.debug("consult_expert failed", exc_info=True)
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def check_consult_expert_requirements() -> bool:
    """Offer the tool only when explicitly enabled."""
    try:
        return os.getenv("HERMES_EXPERT_CONSULT") == "1"
    except Exception:
        return False


CONSULT_EXPERT_SCHEMA = {
    "name": "consult_expert",
    "description": (
        "Consult a slower, stronger expert model (Sakana fugu-ultra) for a genuinely hard "
        "reasoning or coding sub-problem. Use only when the sub-problem is difficult enough "
        "to justify expert escalation; do not use for routine lookups, simple coding edits, "
        "or tasks the current agent can answer directly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The hard reasoning/coding sub-problem to ask the expert model.",
            },
            "context": {
                "type": "string",
                "description": "Optional concise context, constraints, code snippets, or error output needed to answer.",
                "default": "",
            },
        },
        "required": ["question"],
    },
}


# --- Registry ---
# Registered unconditionally at import; availability is controlled at runtime via check_fn.
# discover_builtin_tools() only imports modules with a top-level registry.register(...) AST
# expression, so the small proxy keeps registration fail-open without hiding that call.
def _register(**kwargs: Any) -> None:
    try:
        from tools.registry import registry as real_registry

        real_registry.register(**kwargs)
    except Exception:
        logger.debug("consult_expert registration skipped", exc_info=True)


class _RegistryProxy:
    def register(self, **kwargs: Any) -> None:
        _register(**kwargs)


registry = _RegistryProxy()
registry.register(
    name="consult_expert",
    toolset="consult_expert",
    schema=CONSULT_EXPERT_SCHEMA,
    handler=lambda args, **kw: consult_expert(
        args.get("question") or "",
        args.get("context") or "",
        db=kw.get("db"),
    ),
    check_fn=check_consult_expert_requirements,
    emoji="🧠",
)
