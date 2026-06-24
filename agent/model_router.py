"""Model router — flag-gated (``HERMES_MODEL_ROUTER=1``), fail-open.

The agent already has a complete, cache-safe model FALLBACK mechanism
(``try_activate_fallback`` swaps model+provider on a terminal error and
``_restore_primary_runtime`` snaps back to the primary each turn). The gap is that
the fallback CHAIN ships empty, so when the primary model rate-limits / overloads
(a recurring pain with glm-5.2:cloud) the turn just fails with no backup.

This module fills that chain automatically from the models actually available to the
primary's provider, so the proven fallback path has somewhere to go. It is:
  * failure-only (never changes the model on a successful turn) → prompt caching is
    untouched in normal operation;
  * flag-gated + fail-open: a no-op unless ``HERMES_MODEL_ROUTER=1``; any error leaves
    the existing (possibly empty) chain exactly as it was;
  * non-destructive: it ONLY populates an EMPTY chain — an explicitly configured
    ``fallback_providers`` chain is always respected, never overwritten.

Per-turn success-path routing is deliberately NOT done here: switching the main model
mid-session invalidates the cached prefix. Session-start routing by task type is a
possible future addition but needs the first user message + a re-snapshot of the
primary runtime; it is intentionally out of scope so this module ships no dead code.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Models we never want as a chat fallback (embedding-only, etc.).
_NON_CHAT_MARKERS = ("embed", "embedding", "reranker", "rerank", "whisper", "tts")
_MAX_CHAIN = 4  # cap the auto-built chain — beyond a few backups adds latency, not safety


def is_enabled() -> bool:
    return os.getenv("HERMES_MODEL_ROUTER") == "1"


def _identity(provider, model, base_url) -> tuple:
    return (
        str(provider or "").strip().lower(),
        str(model or "").strip().lower(),
        str(base_url or "").strip().rstrip("/").lower(),
    )


def _base_model(model) -> str:
    """Model name without its routing/size tag (everything before the first ':').

    Lets us treat ``glm-5.2:cloud`` and bare ``glm-5.2`` as the same model so we never
    add the primary back as a (pointless) same-model fallback under a different tag.
    """
    return str(model or "").split(":")[0].strip().lower()


def _is_chat_model(name: str) -> bool:
    n = (name or "").lower()
    return bool(n) and not any(m in n for m in _NON_CHAT_MARKERS)


def parse_configured_pool() -> List[Dict]:
    """Read an explicit candidate pool from ``HERMES_ROUTER_MODELS`` (JSON list of
    ``{"provider","model","base_url"?}``). Returns [] if unset/invalid (→ fall back to
    provider discovery)."""
    raw = (os.getenv("HERMES_ROUTER_MODELS") or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        out = []
        for e in data:
            if isinstance(e, dict) and e.get("provider") and e.get("model"):
                out.append({k: e[k] for k in ("provider", "model", "base_url") if e.get(k)})
        return out
    except Exception:
        logger.debug("HERMES_ROUTER_MODELS parse failed", exc_info=True)
        return []


# Short per-probe timeout: discovery runs synchronously at agent INIT and tries up to
# 3 endpoints, so a long timeout on a firewalled host could stack into a multi-second
# init stall. 2s keeps worst-case discovery ≈6s; a refused port fails instantly.
_PROBE_TIMEOUT = 2.0


def _default_fetch(url: str) -> Optional[dict]:
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def discover_models(provider: str, base_url: str,
                    _fetch: Optional[Callable[[str], Optional[dict]]] = None) -> List[str]:
    """Discover chat model names reachable for an Ollama-style provider via /api/tags.

    Tries the provider's own host plus the common local Ollama ports. Returns chat
    model names (embedding/non-chat filtered out). [] for non-Ollama providers or on
    any error — fail-open, never raises.
    """
    fetch = _fetch or _default_fetch
    prov = (provider or "").lower()
    bu = (base_url or "").lower()
    looks_ollama = "ollama" in prov or "ollama" in bu or "11434" in bu or "11500" in bu
    if not looks_ollama:
        return []  # discovery only implemented for the local-Ollama case

    urls: List[str] = []
    # Derive host root from base_url (strip an OpenAI-compat /v1 suffix).
    try:
        if base_url:
            from urllib.parse import urlparse
            p = urlparse(base_url if "://" in base_url else "http://" + base_url)
            if p.scheme and p.netloc:
                urls.append(f"{p.scheme}://{p.netloc}/api/tags")
    except Exception:
        logger.debug("base_url parse failed in discover_models", exc_info=True)
    urls += ["http://localhost:11500/api/tags", "http://localhost:11434/api/tags"]

    seen_url = set()
    for url in urls:
        if url in seen_url:
            continue
        seen_url.add(url)
        data = fetch(url)
        if not isinstance(data, dict):
            continue
        models = data.get("models")
        if not isinstance(models, list):
            continue
        names: List[str] = []
        for m in models:
            name = m.get("name") or m.get("model") if isinstance(m, dict) else None
            if name and _is_chat_model(name):
                names.append(name)
        if names:
            return names
    return []


def _order_for_fallback(names: List[str]) -> List[str]:
    """Stable order favouring versatile/coding-capable models first (best generic
    backup), preserving original order within each tier."""
    def tier(n: str) -> int:
        ln = n.lower()
        if "code" in ln or "coder" in ln:
            return 0
        if any(t in ln for t in ("glm", "kimi", "minimax", "nemotron", "qwen", "deepseek", "llama")):
            return 1
        return 2
    return sorted(names, key=lambda n: (tier(n),))  # sorted is stable → ties keep order


def recommend_fallback_chain(primary: Dict, pool: List[Dict]) -> List[Dict]:
    """Pure: build a fallback chain from a candidate ``pool``, excluding the primary
    and any duplicates, ordered for generic resilience, capped at ``_MAX_CHAIN``."""
    prim_id = _identity(primary.get("provider"), primary.get("model"), primary.get("base_url"))
    prim_base = _base_model(primary.get("model"))
    seen = {prim_id}
    chain: List[Dict] = []
    for entry in pool:
        # Skip any entry that is the SAME model family as the primary (any tag) — a
        # same-model fallback is pointless (same upstream → same overload).
        if _base_model(entry.get("model")) == prim_base:
            continue
        ident = _identity(entry.get("provider"), entry.get("model"), entry.get("base_url"))
        if ident in seen:
            continue
        seen.add(ident)
        chain.append(entry)
        if len(chain) >= _MAX_CHAIN:
            break
    return chain


def build_pool_from_names(provider: str, model_names: List[str], base_url: str) -> List[Dict]:
    """Turn discovered model names into chain entries on the SAME route as the primary
    (so they are reachable with the same creds/endpoint), ordered for fallback."""
    ordered = _order_for_fallback(model_names)
    pool = []
    for name in ordered:
        entry = {"provider": provider, "model": name}
        if base_url:
            entry["base_url"] = base_url
        pool.append(entry)
    return pool


def maybe_populate_fallback_chain(agent) -> int:
    """If routing is enabled and the agent's fallback chain is EMPTY, auto-fill it from
    a configured pool (``HERMES_ROUTER_MODELS``) or, failing that, from models discovered
    on the primary's Ollama provider. Returns the number of fallback entries added.

    Fail-open: returns 0 and leaves the chain untouched on any error, when disabled, or
    when a chain is already configured (explicit config always wins).
    """
    if not is_enabled():
        return 0
    try:
        existing = getattr(agent, "_fallback_chain", None)
        if existing:  # respect an explicitly-configured chain — never overwrite
            return 0
        provider = getattr(agent, "provider", "") or ""
        model = getattr(agent, "model", "") or ""
        base_url = getattr(agent, "base_url", "") or ""

        pool = parse_configured_pool()
        if not pool:
            names = discover_models(provider, base_url)
            pool = build_pool_from_names(provider, names, base_url)
        if not pool:
            return 0

        chain = recommend_fallback_chain(
            {"provider": provider, "model": model, "base_url": base_url}, pool
        )
        if not chain:
            return 0

        agent._fallback_chain = chain
        agent._fallback_index = 0
        agent._fallback_activated = getattr(agent, "_fallback_activated", False)
        agent._fallback_model = chain[0]
        logger.debug("model_router populated fallback chain: %s",
                     [c.get("model") for c in chain])
        return len(chain)
    except Exception:
        logger.debug("maybe_populate_fallback_chain failed", exc_info=True)
        return 0
