#!/usr/bin/env python3
"""skill_search — find the most relevant skill(s) by MEANING, not lexical eyeballing.

The agent has dozens of skills whose names/descriptions sit in a static list the model
must scan every turn. When a task is worded differently from a skill's description, the
right skill is easy to miss. This tool ranks skills for a task query using HYBRID recall:
a lightweight keyword pass fused (RRF) with semantic vector similarity over each skill's
name+description, reusing the isolated semantic-recall sidecar index (kind="skill").

Safety / isolation:
  * NEW file only — no hot-path edits.
  * Registered ONLY when ``HERMES_SEMANTIC_RECALL=1`` (registry-gated) so the default tool
    list / prompt cache is byte-identical when the feature is off.
  * Reuses the isolated ``state.db.semantic.db`` sidecar (own conn+lock) — never the live
    state.db connection.
  * Fail-open: with no embedder/index it degrades to the keyword ranking; never raises.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> List[str]:
    return _WORD.findall((text or "").lower())


def _keyword_rank(skills: List[Dict[str, Any]], query: str) -> List[str]:
    """Always-available lexical fallback: rank skill names by query-term overlap
    (name matches weighted higher than description). Stable, returns ALL skills ordered."""
    q = set(_tokens(query))
    scored = []
    for i, s in enumerate(skills):
        name = s.get("name", "")
        name_toks = set(_tokens(name))
        desc_toks = set(_tokens(s.get("description", "")))
        score = 2 * len(q & name_toks) + len(q & desc_toks)
        scored.append((score, i, name))
    # higher score first; ties keep discovery order (stable)
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [name for _, _, name in scored]


def _load_skills() -> List[Dict[str, Any]]:
    try:
        from tools.skills_tool import _find_all_skills
        return _find_all_skills() or []
    except Exception:
        logger.debug("skill_search: _find_all_skills failed", exc_info=True)
        return []


def skill_search(query: str = "", limit: int = 5, db=None) -> str:
    """Return the skills most relevant to ``query``, best-first.

    Hybrid (keyword + semantic) when semantic recall is available; keyword-only otherwise.
    """
    if not isinstance(query, str) or not query.strip():
        return json.dumps({"success": False, "error": "query is required"}, ensure_ascii=False)

    skills = [s for s in _load_skills() if s.get("name")]
    if not skills:
        return json.dumps({"success": True, "query": query, "results": [],
                           "message": "No skills found."}, ensure_ascii=False)

    by_name = {s["name"]: s for s in skills}
    kw_ranked = _keyword_rank(skills, query)
    ranking = kw_ranked
    mode = "keyword"

    # Semantic augmentation (fail-open).
    try:
        from agent import semantic_recall as sr
        if sr.is_enabled() and db is not None:
            rec = sr.SemanticRecall.for_db(db)
            if rec.available:
                # Lazily (re)index skills — hash-skip makes repeat calls cheap.
                rec.index_many("skill", [(s["name"], f"{s['name']}: {s.get('description','')}")
                                         for s in skills])
                # Ground scoring in the LIVE skill set so stale sidecar rows (deleted /
                # renamed skills) can't consume rank positions ahead of real ones.
                vec = rec.vector_search("skill", query, k=len(skills),
                                        candidate_ids=[s["name"] for s in skills])
                if vec:
                    vec_ids = [vid for vid, _ in vec]
                    fused = sr.rrf_fuse([kw_ranked, vec_ids])
                    ranking = [rid for rid, _ in fused]
                    # keep any skill missing from the fused set (append in keyword order)
                    seen = set(ranking)
                    ranking += [n for n in kw_ranked if n not in seen]
                    mode = "hybrid"
    except Exception:
        logger.debug("skill_search: semantic augmentation skipped", exc_info=True)

    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 5

    results = []
    for name in ranking[:limit]:
        s = by_name.get(name)
        if not s:
            continue
        results.append({
            "name": name,
            "category": s.get("category", ""),
            "description": s.get("description", ""),
        })

    return json.dumps({
        "success": True,
        "query": query,
        "mode": mode,
        "results": results,
        "count": len(results),
        "hint": "Load a skill with skill_view(name) to use it.",
    }, ensure_ascii=False)


def check_skill_search_requirements() -> bool:
    """Gate availability at RUNTIME (not import): offered only when semantic recall is
    enabled AND the skills subsystem is present. Evaluating the flag here (via check_fn,
    which the registry calls when building the offered tool list) instead of at import
    time makes it correct regardless of when .env is applied to os.environ."""
    try:
        from agent import semantic_recall as sr
        if not sr.is_enabled():
            return False
        from tools.skills_tool import check_skills_requirements
        return check_skills_requirements()
    except Exception:
        return False


SKILL_SEARCH_SCHEMA = {
    "name": "skill_search",
    "description": (
        "Find the skill(s) most relevant to a task by MEANING (hybrid keyword + semantic "
        "search over all skills' names/descriptions), instead of scanning the full skill "
        "list by eye. Pass a natural-language description of what you're trying to do; "
        "returns the top matching skills ranked best-first. Then load the chosen one with "
        "skill_view(name). Use this when you suspect a skill exists for the task but aren't "
        "sure of its exact name."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language description of the task / what you need a skill for.",
            },
            "limit": {
                "type": "integer",
                "description": "Max skills to return (default 5, max 20).",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}


# --- Registry ---
# Registered UNCONDITIONALLY (tool discovery runs at import, before .env is applied to
# os.environ — gating registration on the flag here would race and lose). The flag is
# enforced at RUNTIME via check_fn (check_skill_search_requirements), which the registry
# evaluates when building the offered tool list: flag off → excluded → default tool list
# byte-identical. Uses its OWN toolset (like session_search) so it never claims the
# "skills" toolset-check slot (which would otherwise disable skills_list/skill_view when
# the flag is off).
def _register() -> None:
    try:
        from tools.registry import registry
        registry.register(
            name="skill_search",
            toolset="skill_search",
            schema=SKILL_SEARCH_SCHEMA,
            handler=lambda args, **kw: skill_search(
                query=args.get("query") or "",
                limit=args.get("limit", 5),
                db=kw.get("db"),
            ),
            check_fn=check_skill_search_requirements,
            emoji="🧭",
        )
    except Exception:
        logger.debug("skill_search registration skipped", exc_info=True)


_register()
