#!/usr/bin/env python3
"""Keyless SAFE additive SQLite migrator: dry-run on a copy, verify, then apply.

stdlib only (sqlite3, shutil, json). No external service, no API key.

Usage:
    python safe_migrate.py LIVE.db migration.json            # dry-run (default, live untouched)
    python safe_migrate.py LIVE.db migration.json --apply    # apply to live after a passing dry-run

migration.json schema:
{
  "columns": [
    {"table": "users", "name": "email",  "type": "TEXT"},
    {"table": "users", "name": "active", "type": "INTEGER", "default": 1}
  ],
  "indexes": [
    {"name": "idx_users_email", "table": "users", "columns": ["email"], "unique": true}
  ]
}

Rules enforced:
  - additive only (ALTER TABLE ADD COLUMN; never DROP/RENAME/retype)
  - table- AND column-aware via PRAGMA table_info (real table, not assumptions)
  - COLUMNS run BEFORE INDEXES (an index on a not-yet-added column aborts setup)
  - idempotent (existing columns/indexes are skipped, safe to re-run)
  - ALWAYS dry-runs on a COPY first; live is backed up before --apply
  - verifies per-table row counts preserved + PRAGMA integrity_check == ok
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile


def table_exists(con, t):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
    ).fetchone() is not None


def column_exists(con, t, c):
    return c in {r[1] for r in con.execute(f"PRAGMA table_info('{t}')")}


def index_exists(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone() is not None


def rowcounts(con):
    out = {}
    for (t,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        out[t] = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    return out


def quote_default(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def run_migration(db_path, plan):
    con = sqlite3.connect(db_path)
    try:
        before = rowcounts(con)
        applied = []
        # 1. COLUMNS FIRST (additive, idempotent, table+column aware)
        for col in plan.get("columns", []):
            t, n, ty = col["table"], col["name"], col.get("type", "TEXT")
            if not table_exists(con, t):
                raise RuntimeError(
                    f"table '{t}' does not exist (additive migrator won't CREATE it)"
                )
            if column_exists(con, t, n):
                continue  # idempotent skip
            ddl = f'ALTER TABLE "{t}" ADD COLUMN "{n}" {ty}'
            d = quote_default(col.get("default"))
            if d is not None:
                ddl += f" DEFAULT {d}"
            con.execute(ddl)
            applied.append(ddl)
        # 2. INDEXES SECOND (the new columns now exist)
        for idx in plan.get("indexes", []):
            if index_exists(con, idx["name"]):
                continue
            for c in idx["columns"]:
                if not column_exists(con, idx["table"], c):
                    raise RuntimeError(
                        f"index '{idx['name']}' references missing column "
                        f"{idx['table']}.{c} -- add it in 'columns' first"
                    )
            uniq = "UNIQUE " if idx.get("unique") else ""
            cols = ", ".join(f'"{c}"' for c in idx["columns"])
            ddl = (
                f'CREATE {uniq}INDEX IF NOT EXISTS "{idx["name"]}" '
                f'ON "{idx["table"]}" ({cols})'
            )
            con.execute(ddl)
            applied.append(ddl)
        con.commit()
        after = rowcounts(con)
        integ = con.execute("PRAGMA integrity_check").fetchone()[0]
        if before != after:
            raise RuntimeError(f"row counts changed! before={before} after={after}")
        if integ != "ok":
            raise RuntimeError(f"integrity_check failed: {integ}")
        return applied, before, after
    finally:
        con.close()


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    live, plan_path = sys.argv[1], sys.argv[2]
    apply = "--apply" in sys.argv[3:]
    with open(plan_path) as f:
        plan = json.load(f)

    # ---- DRY RUN on a copy, ALWAYS first ----
    fd, copy = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    shutil.copy(live, copy)
    try:
        applied, before, after = run_migration(copy, plan)
    except Exception as e:
        os.remove(copy)
        sys.exit(f"DRY-RUN FAILED on copy, live untouched: {e}")
    print("DRY-RUN OK on copy:", copy)
    for s in applied:
        print("  ", s)
    print(f"  rows before={before} after={after} (preserved)")
    os.remove(copy)

    if not apply:
        print("Dry-run only. Re-run with --apply to migrate the live DB.")
        return

    # ---- APPLY to live (back it up first) ----
    bak = live + ".bak"
    shutil.copy(live, bak)
    print("backed up live ->", bak)
    applied, before, after = run_migration(live, plan)
    print("APPLIED to live:")
    for s in applied:
        print("  ", s)
    print(f"  rows before={before} after={after} (preserved)")


if __name__ == "__main__":
    main()
