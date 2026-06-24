"""Offline learning cycle — the single real integration of Learning Governance V1.

This ties the previously-isolated learning modules into one working feedback
loop. Before this module, every learning component (evidence builder,
effectiveness manager, governance engine, strategic learning) had green unit
tests but **no production caller** — they formed a self-consistent island that
the live agent never touched. This is the bridge:

    OPVAL evidence  ->  observations  ->  effectiveness  ->  governance decision
                                                                    |
                                          applied authority  <------+

Two entry points, both gated by ``HERMES_LEARNING=1`` (defense in depth on top
of the ``HERMES_OPVAL=1`` gate that produces the evidence in the first place):

* :func:`run_learning_cycle` — the offline digest. Reads eligible OPVAL
  sessions, derives + persists observations, evaluates effectiveness per
  strategy, and records an append-only governance decision when a strategy is
  promotion- or retirement-eligible. This is pure off-hot-loop work; it is safe
  to call at session end or from a scheduler tick. It never mutates a live
  conversation.

* :func:`apply_learned_authority` — applied ONCE at session start, before the
  system prompt and tool list are built for the session. It loads the latest
  valid governance directive for the session's domain and applies only the
  cache-safe / semantically-safe authority knobs to the live agent.

Cache invariant (sacred): applying authority at *session start* is safe because
the prompt-cache prefix and tool list for the conversation are established
afterward, once, from the post-application state. We deliberately never apply
``tool_scope`` here — it would filter the cached tool list, and the current
retirement directive carries a placeholder value. Only PROMOTE/ENFORCE events
are ever applied to a live agent; RETIRE is a "stop trusting this strategy"
signal, not something to push onto a running session.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from agent import learning_constants as lc

logger = logging.getLogger(__name__)

__all__ = [
    "learning_enabled",
    "strategy_id_for_domain",
    "run_learning_cycle",
    "apply_learned_authority",
    "recent_domain",
    "maybe_run_session_end_digest",
]

# How often the autonomous session-end digest is allowed to run (seconds).
_DIGEST_THROTTLE_SECONDS = 3600
# Only look back this far when digesting at session end (keeps the scan cheap;
# the indexed opval_sessions.start_time query stays fast).
_DIGEST_LOOKBACK_SECONDS = 7 * 24 * 3600

# Authority knobs that are safe to apply to a live agent at session start.
# ``tool_scope`` is intentionally excluded: it filters the cached tool list and
# the retirement directive currently carries a placeholder value. The remaining
# knobs only affect delegation policy and the compressor threshold, neither of
# which mutates the byte-stable system prompt.
_SESSION_START_SAFE_KNOBS = (
    "policy",
    "context_size_override",
    "allow_self_delegate",
    "max_tool_iterations",
)

# Governance event types that may be applied to a live agent.
_APPLICABLE_EVENT_TYPES = (lc.GOVERNANCE_EVENT_PROMOTE, lc.GOVERNANCE_EVENT_ENFORCE)
_APPLICABLE_STATUSES = (lc.GOVERNANCE_STATUS_APPLIED, lc.GOVERNANCE_STATUS_PENDING)


def learning_enabled() -> bool:
    """True when the learning cycle is enabled (``HERMES_LEARNING=1``)."""
    return os.getenv("HERMES_LEARNING") == "1"


def strategy_id_for_domain(domain: Optional[str]) -> str:
    """Canonical strategy id for an OPVAL primary domain.

    This is the single source of truth for the domain->strategy mapping; every
    component that needs a strategy id (evidence builder construction,
    effectiveness lookup, governance, authority application) routes through
    here so ids reconcile across the pipeline.
    """
    return f"strategy:{domain or 'default'}"


def run_learning_cycle(
    conn: Any,
    since: float = 0.0,
    state_db_conn: Any = None,
    min_turns: int = 3,
    min_tool_executions: int = 1,
) -> Dict[str, Any]:
    """Run one offline learning digest over eligible OPVAL sessions.

    Returns a summary dict (sessions processed, observations persisted, and a
    per-strategy decision record). Does not touch any live conversation.
    """
    from agent.opval.store import OpvalStore
    from agent.learning_evidence_builder import LearningEvidenceBuilder
    from agent.strategy_effectiveness_manager import StrategyEffectivenessManager
    from agent.learning_governance import LearningGovernance

    store = OpvalStore(conn)  # idempotent schema + row_factory on the conn
    sessions = store.get_eligible_sessions(
        since=since,
        min_turns=min_turns,
        min_tool_executions=min_tool_executions,
        state_db_conn=state_db_conn,
    )

    strategies: set[str] = set()
    observations_persisted = 0
    for session in sessions:
        sid = strategy_id_for_domain(session.get("primary_domain"))
        try:
            builder = LearningEvidenceBuilder(conn, sid)
            turns = store.get_turns(session["session_id"])
            observations_persisted += builder.build_and_persist(session, turns)
            strategies.add(sid)
        except Exception as exc:  # one bad session must not abort the digest
            logger.debug(
                "learning_cycle: skipping session %s: %s",
                session.get("session_id"),
                exc,
            )

    eff_mgr = StrategyEffectivenessManager(conn)
    gov = LearningGovernance(conn)
    decisions: Dict[str, Any] = {}
    for sid in sorted(strategies):
        eff = eff_mgr.evaluate(sid)
        decision = None
        # Idempotency: the governance ledger records state *transitions*, not a
        # fresh event every digest. Skip if the strategy is already in the
        # target state (latest event already PROMOTE/RETIRE accordingly).
        latest = _latest_event_type(conn, sid)
        try:
            if eff.promotion_eligible and latest != lc.GOVERNANCE_EVENT_PROMOTE:
                decision = gov.promote_strategy(sid)
            elif eff.retirement_eligible and latest != lc.GOVERNANCE_EVENT_RETIRE:
                decision = gov.retire_strategy(sid)
        except Exception as exc:
            logger.debug("learning_cycle: governance failed for %s: %s", sid, exc)
        decisions[sid] = {
            "sample_count": eff.sample_count,
            "win_rate": round(eff.win_rate, 4),
            "avg_score": round(eff.avg_score, 2),
            "promotion_eligible": eff.promotion_eligible,
            "retirement_eligible": eff.retirement_eligible,
            "event_type": (
                decision.event.event_type
                if decision and decision.approved and decision.event
                else None
            ),
            "approved": bool(decision and decision.approved),
            "reason": decision.reason if decision else "not eligible",
        }

    summary = {
        "sessions_processed": len(sessions),
        "observations_persisted": observations_persisted,
        "strategies": sorted(strategies),
        "decisions": decisions,
    }
    logger.info(
        "learning_cycle: %d sessions, %d observations, %d strategies, %d decision(s)",
        summary["sessions_processed"],
        summary["observations_persisted"],
        len(strategies),
        sum(1 for d in decisions.values() if d["approved"]),
    )
    return summary


def _latest_event_type(conn: Any, strategy_id: str) -> Optional[str]:
    """Return the most recent governance event type for a strategy, if any."""
    row = conn.execute(
        "SELECT event_type FROM learning_governance_events "
        "WHERE strategy_id=? ORDER BY created_at DESC, event_id DESC LIMIT 1",
        (strategy_id,),
    ).fetchone()
    return row[0] if row else None


def recent_domain(conn: Any) -> Optional[str]:
    """Best-effort 'current' domain: the most recent real OPVAL session's domain.

    Used at session start to pick which learned strategy to apply. The domain
    of the *new* session is not yet known (it is classified from the
    conversation), so we use a continuity heuristic: the user tends to continue
    in the domain they last worked in. Returns None when there is no history.
    """
    from agent.opval.store import OpvalStore

    OpvalStore(conn)
    row = conn.execute(
        "SELECT primary_domain FROM opval_sessions "
        "WHERE synthetic=0 AND primary_domain IS NOT NULL "
        "ORDER BY start_time DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def _latest_applicable_event_id(conn: Any, strategy_id: str) -> Optional[str]:
    """Return the most recent PROMOTE/ENFORCE event id for a strategy, if any."""
    placeholders_types = ", ".join("?" * len(_APPLICABLE_EVENT_TYPES))
    placeholders_status = ", ".join("?" * len(_APPLICABLE_STATUSES))
    row = conn.execute(
        f"SELECT event_id FROM learning_governance_events "
        f"WHERE strategy_id=? AND event_type IN ({placeholders_types}) "
        f"AND status IN ({placeholders_status}) "
        f"ORDER BY created_at DESC, event_id DESC LIMIT 1",
        (strategy_id, *_APPLICABLE_EVENT_TYPES, *_APPLICABLE_STATUSES),
    ).fetchone()
    if not row:
        return None
    return row[0]


def _validated_directive(conn: Any, strategy_id: str, event_id: str):
    """Return the AuthorityDecision for an event, or None if it cannot be trusted.

    Integrity is enforced two ways: the strategy's whole governance chain must
    verify (tamper-evident), and the event status must be applicable.
    """
    from agent.learning_governance import LearningGovernance
    from agent.runtime_authority import AuthorityDecision

    gov = LearningGovernance(conn)
    if not gov.verify_chain(strategy_id):
        logger.warning(
            "learning_cycle: governance chain for %s failed integrity; not applying",
            strategy_id,
        )
        return None

    row = conn.execute(
        "SELECT authority_directive_json, status FROM learning_governance_events WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if not row:
        return None
    directive_json, status = row[0], row[1]
    if status not in _APPLICABLE_STATUSES or not directive_json:
        return None
    try:
        return AuthorityDecision(**json.loads(directive_json))
    except Exception as exc:
        logger.debug("learning_cycle: invalid directive json for %s: %s", event_id, exc)
        return None


def apply_learned_authority(conn: Any, agent: Any, domain: Optional[str]) -> bool:
    """Apply the latest learned authority directive for ``domain`` to ``agent``.

    Call this once at session start (before the system prompt / tool list are
    built). Returns True if a directive was applied. Only the cache-safe knob
    subset is applied; ``tool_scope`` is never applied here.
    """
    from agent.opval.store import OpvalStore
    from agent.runtime_authority import AuthorityDecision, apply_decision

    OpvalStore(conn)  # ensure schema + row_factory
    strategy_id = strategy_id_for_domain(domain)
    event_id = _latest_applicable_event_id(conn, strategy_id)
    if not event_id:
        return False

    directive = _validated_directive(conn, strategy_id, event_id)
    if directive is None:
        return False

    safe_kwargs = {
        knob: getattr(directive, knob)
        for knob in _SESSION_START_SAFE_KNOBS
        if getattr(directive, knob) is not None
    }
    if not safe_kwargs:
        return False

    apply_decision(agent, AuthorityDecision(**safe_kwargs))

    # Persist the applied policy knobs as the per-turn baseline so M1's
    # per-turn reset (ensure_runtime_authority) restores the *learned* policy
    # each turn instead of reverting to hard permissive. Snapshot whatever
    # apply_decision derived (it computes delegation policy from policy /
    # allow_self_delegate). context_size_override / max_tool_iterations are not
    # reset per turn, so they persist on their own and need no baseline.
    agent._authority_policy_baseline = getattr(
        agent, "_authority_policy", lc.AUTHORITY_POLICY_PERMISSIVE
    )
    agent._authority_delegation_baseline = getattr(
        agent, "_authority_delegation_policy", lc.AUTHORITY_POLICY_PERMISSIVE
    )
    agent._authority_allow_self_delegate_baseline = getattr(
        agent, "_authority_allow_self_delegate", True
    )

    logger.info(
        "learning_cycle: applied learned authority for %s: %s", strategy_id, safe_kwargs
    )
    return True


def maybe_run_session_end_digest(
    conn: Any = None,
    now: Optional[float] = None,
    marker_path: Any = None,
) -> Optional[Dict[str, Any]]:
    """Run the learning digest at session end, throttled to once per interval.

    Autonomous path: when ``HERMES_LEARNING=1``, the conversation loop calls
    this after a session is finalized. A filesystem marker under the profile's
    hermes-home throttles it to ``_DIGEST_THROTTLE_SECONDS`` so heavy users do
    not re-digest on every turn. Returns the cycle summary when it ran, else
    None. Always exception-safe — never raises into the caller.

    ``now`` and ``marker_path`` are injectable for tests.
    """
    import time as _time
    from pathlib import Path

    if now is None:
        now = _time.time()
    try:
        if marker_path is not None:
            marker = Path(marker_path)
        else:
            from hermes_constants import get_hermes_home

            marker = get_hermes_home() / "learning" / ".last_digest"
        last = 0.0
        try:
            last = float(marker.read_text().strip())
        except Exception:
            last = 0.0
        if now - last < _DIGEST_THROTTLE_SECONDS:
            return None
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(now))
        if conn is None:
            # Match OPVAL's own store resolution so the digest still runs when no
            # SessionDB connection is attached (e.g. oneshot / script contexts).
            from agent.opval.integration import default_store_path
            from agent.opval.store import OpvalStore

            conn = OpvalStore(default_store_path())._conn
        return run_learning_cycle(conn, since=max(0.0, now - _DIGEST_LOOKBACK_SECONDS))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("learning_cycle: session-end digest skipped: %s", exc)
        return None
