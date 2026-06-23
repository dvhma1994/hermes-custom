"""Skill induction — mine recurring tool-sequence workflows from past sessions and
PROPOSE them as candidate skills (read-only; never auto-creates anything).

Inspired by Agent Workflow Memory / Voyager skill libraries: the patterns an agent
repeats across many tasks are exactly the workflows worth promoting into a named,
reusable skill. This module surfaces those candidates so the operator (or the existing
skill curator) can decide — the human review IS the safety gate, so unlike auto-skill
generation it needs no eval harness to be safe.

Pure, offline, read-only: it reads the ``messages`` table and returns a ranked list of
candidate workflows. It never writes, never runs in the hot loop, and fail-opens to [].
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Tools whose repetition is mechanical, not a "workflow" worth skill-ifying on their own.
# (We still keep them inside multi-tool sequences — just don't propose a lone-tool skill.)
_DEFAULT_MIN_SESSIONS = 4   # a pattern must recur across at least this many sessions
_DEFAULT_NGRAM = (2, 4)     # contiguous sequence lengths to mine
_DEFAULT_TOP_K = 15


def collapse_repeats(seq: Sequence[str]) -> List[str]:
    """Collapse consecutive identical tools (read_file,read_file → read_file) so we mine
    STRUCTURAL workflows (search→read→patch→test), not mere repetition."""
    out: List[str] = []
    for t in seq:
        if not out or out[-1] != t:
            out.append(t)
    return out


def extract_session_sequences(rows: Sequence[Tuple[str, str]]) -> Dict[str, List[str]]:
    """Group ordered ``(session_id, tool_name)`` rows into per-session tool sequences
    (consecutive repeats collapsed). Rows MUST already be ordered by (session_id, id)."""
    seqs: Dict[str, List[str]] = defaultdict(list)
    for sid, tool in rows:
        if tool:
            seqs[str(sid)].append(tool)
    return {sid: collapse_repeats(s) for sid, s in seqs.items()}


def _all_same(ngram: Tuple[str, ...]) -> bool:
    return len(set(ngram)) == 1


def mine_frequent_ngrams(
    sequences: Dict[str, List[str]],
    ngram: Tuple[int, int] = _DEFAULT_NGRAM,
    min_sessions: int = _DEFAULT_MIN_SESSIONS,
) -> List[Dict]:
    """Find contiguous tool n-grams that recur across many DISTINCT sessions.

    Returns ``[{sequence, length, session_support, total_count}]``. Session-support
    (distinct sessions containing the pattern) is the headline metric — a workflow
    repeated across many tasks matters more than one hammered in a single session.
    """
    lo, hi = ngram
    sess_support: Dict[Tuple[str, ...], set] = defaultdict(set)
    total: Dict[Tuple[str, ...], int] = defaultdict(int)
    for sid, seq in sequences.items():
        for n in range(lo, hi + 1):
            for i in range(len(seq) - n + 1):
                g = tuple(seq[i:i + n])
                if _all_same(g):
                    continue  # lone-tool repetition isn't a workflow
                sess_support[g].add(sid)
                total[g] += 1
    out = []
    for g, sids in sess_support.items():
        if len(sids) >= min_sessions:
            out.append({
                "sequence": list(g),
                "length": len(g),
                "session_support": len(sids),
                "total_count": total[g],
            })
    return out


def _subsumed(a: Dict, b: Dict) -> bool:
    """True if a's sequence is a contiguous sub-sequence of b's (so we can prefer the
    longer pattern when support is comparable)."""
    sa, sb = a["sequence"], b["sequence"]
    if len(sa) >= len(sb):
        return False
    return any(sb[i:i + len(sa)] == sa for i in range(len(sb) - len(sa) + 1))


def rank_candidates(candidates: List[Dict], top_k: int = _DEFAULT_TOP_K) -> List[Dict]:
    """Rank by session-support, then length, then total. Drop shorter patterns that are
    subsumed by a longer one with >= as much support (keep the most informative form)."""
    ranked = sorted(
        candidates,
        key=lambda c: (-c["session_support"], -c["length"], -c["total_count"]),
    )
    kept: List[Dict] = []
    for c in ranked:
        if any(_subsumed(c, k) and k["session_support"] >= c["session_support"] for k in kept):
            continue
        kept.append(c)
        if len(kept) >= top_k:
            break
    return kept


def format_report(candidates: List[Dict]) -> str:
    """Human-readable proposal report."""
    if not candidates:
        return "No recurring multi-tool workflows met the support threshold."
    lines = ["Candidate skills (recurring tool workflows) — review before promoting:", ""]
    for i, c in enumerate(candidates, 1):
        flow = " → ".join(c["sequence"])
        lines.append(
            f"{i}. {flow}\n   seen in {c['session_support']} sessions "
            f"({c['total_count']} times total, length {c['length']})"
        )
    return "\n".join(lines)


def propose_skills_from_db(
    db,
    min_sessions: int = _DEFAULT_MIN_SESSIONS,
    top_k: int = _DEFAULT_TOP_K,
    ngram: Tuple[int, int] = _DEFAULT_NGRAM,
    max_rows: int = 200000,
) -> List[Dict]:
    """Read tool-call sequences from the ``messages`` table and return ranked candidate
    workflows. Read-only + fail-open → [] on any error. ``db`` is a SessionDB-like with
    a ``_conn``."""
    try:
        conn = getattr(db, "_conn", None)
        if conn is None:
            return []
        rows = conn.execute(
            "SELECT session_id, tool_name FROM messages "
            "WHERE tool_name IS NOT NULL AND tool_name != '' "
            "ORDER BY session_id, id LIMIT ?",
            (int(max_rows),),
        ).fetchall()
        sequences = extract_session_sequences([(r[0], r[1]) for r in rows])
        candidates = mine_frequent_ngrams(sequences, ngram=ngram, min_sessions=min_sessions)
        return rank_candidates(candidates, top_k=top_k)
    except Exception:
        logger.debug("propose_skills_from_db failed", exc_info=True)
        return []
