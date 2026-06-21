#!/usr/bin/env python3
"""Hermes learning-loop status dashboard.

Makes the self-improvement loop OBSERVABLE: how much telemetry has accumulated,
what the loop has learned, governance activity, and progress toward the readiness
gates that unlock authority changes. Read-only — safe to run anytime.

    python scripts/learning_status.py            # show the dashboard
    python scripts/learning_status.py --cycle    # also run one offline digest and show decisions

Readiness gates (defaults): >=50 real sessions, >=5 per domain, drift <=15%,
misalignment <=10%, score >=80. The loop only applies authority changes once met.
"""
import argparse
import datetime as _dt
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

GATE_TOTAL = 50
GATE_PER_DOMAIN = 5


def _store_path() -> str:
    try:
        from agent.opval.integration import default_store_path
        return str(default_store_path())
    except Exception:
        home = os.environ.get("HERMES_HOME") or os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes")
        return os.path.join(home, "state.db")


def _ro_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _has(conn, table) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _scalar(conn, q, args=()):
    try:
        r = conn.execute(q, args).fetchone()
        return r[0] if r else None
    except sqlite3.Error:
        return None


def _ago(ts):
    if not ts:
        return "never"
    try:
        delta = _dt.datetime.now().timestamp() - float(ts)
        h = delta / 3600
        if h < 1:
            return f"{int(delta/60)}m ago"
        if h < 48:
            return f"{h:.1f}h ago"
        return f"{h/24:.1f}d ago"
    except Exception:
        return str(ts)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Hermes learning-loop status")
    ap.add_argument("--cycle", action="store_true", help="also run one offline digest")
    a = ap.parse_args(argv)

    path = _store_path()
    print(f"learning store: {path}")
    print(f"HERMES_OPVAL={os.environ.get('HERMES_OPVAL','(unset)')}  "
          f"HERMES_LEARNING={os.environ.get('HERMES_LEARNING','(unset)')}")
    if not os.path.exists(path):
        print("  no state.db yet — run the agent at least once.")
        return 1
    conn = _ro_conn(path)

    # ── Telemetry ────────────────────────────────────────────────────────
    print("\n── OPVAL telemetry ──")
    if _has(conn, "opval_sessions"):
        total = _scalar(conn, "SELECT COUNT(*) FROM opval_sessions") or 0
        latest = _scalar(conn, "SELECT MAX(recorded_at) FROM opval_sessions")
        turns = _scalar(conn, "SELECT COUNT(*) FROM opval_turns") if _has(conn, "opval_turns") else 0
        print(f"  sessions: {total}   turns: {turns}   latest: {_ago(latest)}")
        try:
            rows = conn.execute(
                "SELECT primary_domain AS d, COUNT(*) c FROM opval_sessions "
                "GROUP BY primary_domain ORDER BY c DESC LIMIT 8").fetchall()
            if rows:
                print("  by domain: " + ", ".join(f"{r['d'] or '?'}={r['c']}" for r in rows))
        except sqlite3.Error:
            pass
    else:
        print("  (opval_sessions table not present)")

    # ── What it has learned ──────────────────────────────────────────────
    print("\n── learning ──")
    obs = _scalar(conn, "SELECT COUNT(*) FROM learning_strategy_observations") \
        if _has(conn, "learning_strategy_observations") else None
    strat = _scalar(conn, "SELECT COUNT(*) FROM learning_strategies") \
        if _has(conn, "learning_strategies") else None
    print(f"  observations: {obs if obs is not None else 'n/a'}   "
          f"strategies: {strat if strat is not None else 'n/a'}")
    if _has(conn, "learning_governance_events"):
        gov = _scalar(conn, "SELECT COUNT(*) FROM learning_governance_events") or 0
        print(f"  governance events: {gov}")
        try:
            rows = conn.execute(
                "SELECT event_type AS t, COUNT(*) c FROM learning_governance_events "
                "GROUP BY event_type ORDER BY c DESC").fetchall()
            if rows:
                print("    " + ", ".join(f"{r['t']}={r['c']}" for r in rows))
        except sqlite3.Error:
            pass

    # ── Readiness gates ──────────────────────────────────────────────────
    print("\n── readiness gates ──")
    if _has(conn, "opval_sessions"):
        total = _scalar(conn, "SELECT COUNT(*) FROM opval_sessions") or 0
        domains_ok = _scalar(
            conn,
            "SELECT COUNT(*) FROM (SELECT primary_domain FROM opval_sessions "
            "GROUP BY primary_domain HAVING COUNT(*) >= ?)", (GATE_PER_DOMAIN,)) or 0
        ndom = _scalar(conn, "SELECT COUNT(DISTINCT primary_domain) FROM opval_sessions") or 0
        bar = lambda n, d: f"[{'#'*min(20,int(20*n/d))}{'.'*(20-min(20,int(20*n/d)))}] {n}/{d}"
        print(f"  sessions    {bar(total, GATE_TOTAL)}")
        print(f"  domains>={GATE_PER_DOMAIN}   {bar(domains_ok, max(1,ndom))}  "
              f"({domains_ok} of {ndom} domains have >= {GATE_PER_DOMAIN} sessions)")
        met = total >= GATE_TOTAL
        print(f"  -> authority changes {'UNLOCKED' if met else 'gated (accumulating)'}")
    conn.close()

    # ── Optional: run one digest ─────────────────────────────────────────
    if a.cycle:
        print("\n── offline digest (run_learning_cycle) ──")
        try:
            from agent.opval.store import OpvalStore
            from agent.learning_cycle import run_learning_cycle
            store = OpvalStore(path)
            res = run_learning_cycle(store._conn, min_turns=1, min_tool_executions=0)
            print(f"  sessions_processed: {res.get('sessions_processed')}  "
                  f"observations_persisted: {res.get('observations_persisted')}")
            for sid, dec in (res.get("decisions") or {}).items():
                print(f"  {sid}: win_rate={dec.get('win_rate')} "
                      f"avg_score={dec.get('avg_score')} "
                      f"event={dec.get('event_type')} reason={dec.get('reason')}")
        except Exception as e:
            print(f"  digest unavailable: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
