---
name: schema-migrate
description: Safely evolve a SQLite schema with additive, idempotent, table-aware migrations dry-run on a copy before touching the live DB.
version: 1.0.0
author: Hermes Agent + Claude
metadata:
  hermes:
    tags: [sqlite, migration, schema, database, ddl]
    category: software-development
---

# Schema Migrate Skill

Evolve a SQLite (or any SQL) schema **safely**: only add things, never break
the live file. This skill encodes the five rules that prevent the classic
self-inflicted outage and one specific ordering bug that aborts schema setup.

Keyless: uses only the `sqlite3` CLI and the Python stdlib `sqlite3` module. No
external service, no API key, no network.

## The Five Rules

1. **Additive only.** `ALTER TABLE ... ADD COLUMN`. Never `DROP COLUMN`,
   `RENAME`, or retype in place â€” those rewrite the table and can lose data.
2. **Idempotent + table-aware.** Before altering, ask the *real* table what it
   has via `PRAGMA table_info`. Skip anything already present so the migration
   is safe to re-run.
3. **Columns BEFORE indexes.** Run every `ADD COLUMN` first, *then* create any
   index that references a new column. (This is the bug below.)
4. **Dry-run on a COPY first.** Always apply to a throwaway copy of the live DB
   and verify it succeeds before touching the real file.
5. **Verify row counts preserved.** Count rows per table before and after; an
   additive migration must not change any count. Also run `PRAGMA integrity_check`.

## When to Use

- Adding columns/indexes to a SQLite DB an app already created and populated.
- Schema "setup" / "ensure" code that runs on every boot and must be safe to
  re-run against an existing DB.
- Any migration where losing the live data would hurt and you can't take the
  app fully offline.

## When NOT to Use

- **Destructive changes** (drop/rename column, change a type, split a table).
  Those need a full table rebuild (`CREATE new` â†’ `INSERT ... SELECT` â†’ swap),
  not this additive flow.
- **A real migration framework already owns the schema** (Alembic, Prisma,
  Rails, golang-migrate). Use it; don't hand-ALTER underneath it.
- Non-SQLite engines where DDL is transactional and online (Postgres) â€” the
  ordering rule still helps, but the "copy the file" step does not apply.

## The Ordering Bug (why rule 3 exists)

Setup scripts run with `.bail on`, so the **first** error aborts the whole
script and later statements never run. Putting the index before the column does
exactly that:

```sql
-- bad.sql  (run as: sqlite3 app.db < bad.sql)
.bail on
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);  -- email doesn't exist yet
ALTER TABLE users ADD COLUMN email TEXT;                      -- never reached
```

```
$ sqlite3 app.db < bad.sql
Parse error near line 2: no such column: email
```

The script aborts, `email` is **not** added, and every later table in the same
setup file is left unmigrated. `IF NOT EXISTS` does **not** save you â€” the
column reference is resolved before the index existence check. Fix: add the
column first.

## Procedure (manual, CLI)

Run this against a **copy**, then promote only if it passes.

```bash
DB=app.db

# 0. Snapshot the live DB into a copy (atomic, consistent, incl. WAL).
sqlite3 "$DB" "VACUUM INTO 'migrate_test.db'"
COPY=migrate_test.db

# 1. Baseline row counts on the copy (one generated query, no fragile loop).
#    Generate a per-table COUNT statement with SQLite itself, then run it.
gen() { sqlite3 "$1" "SELECT 'SELECT '''||name||'=''||COUNT(*) FROM \"'||name||'\";' \
        FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"; }
snapshot() { gen "$1" | sqlite3 "$1"; }
BEFORE=$(snapshot "$COPY"); echo "$BEFORE"

# 2. Check the REAL table before altering (table-aware idempotency).
sqlite3 "$COPY" "PRAGMA table_info(users)"      # column 2 = name
has_col() { sqlite3 "$1" "PRAGMA table_info($2)" | cut -d'|' -f2 | grep -qx "$3"; }

# 3. COLUMNS FIRST, then the index. Both idempotent.
has_col "$COPY" users email || sqlite3 "$COPY" "ALTER TABLE users ADD COLUMN email TEXT"
sqlite3 "$COPY" "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)"

# 4. Verify on the copy: counts preserved + integrity ok.
AFTER=$(snapshot "$COPY"); echo "$AFTER"
sqlite3 "$COPY" "PRAGMA integrity_check"        # must print: ok
[ "$BEFORE" = "$AFTER" ] || { echo "ROW COUNTS CHANGED -- abort"; exit 1; }

# 5. Only now apply the SAME steps to the live DB (back it up first).
cp "$DB" "$DB.bak"
has_col "$DB" users email || sqlite3 "$DB" "ALTER TABLE users ADD COLUMN email TEXT"
sqlite3 "$DB" "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)"
```

## Procedure (scripted helper)

`scripts/safe_migrate.py` (stdlib only) does all five rules from a JSON plan:
it copies the DB, runs columns-then-indexes, refuses an index on a missing
column, checks row counts + integrity on the copy, and only touches the live
file (after backing it up) when you pass `--apply`.

```bash
cat > mig.json <<'JSON'
{
  "columns": [
    {"table": "users", "name": "email",  "type": "TEXT"},
    {"table": "users", "name": "active", "type": "INTEGER", "default": 1}
  ],
  "indexes": [
    {"name": "idx_users_email", "table": "users", "columns": ["email"], "unique": true}
  ]
}
JSON

python scripts/safe_migrate.py app.db mig.json            # dry-run on a copy, live untouched
python scripts/safe_migrate.py app.db mig.json --apply    # apply to live after a passing dry-run
```

Dry-run output (live unchanged):

```
DRY-RUN OK on copy: /tmp/tmp7qtr03d6.db
   ALTER TABLE "users" ADD COLUMN "email" TEXT
   ALTER TABLE "users" ADD COLUMN "active" INTEGER DEFAULT 1
   CREATE UNIQUE INDEX IF NOT EXISTS "idx_users_email" ON "users" ("email")
  rows before={'logs': 1, 'users': 3} after={'logs': 1, 'users': 3} (preserved)
```

Re-running `--apply` is a no-op (idempotent). A plan whose index names a column
not in `columns` is refused on the copy, live untouched:

```
DRY-RUN FAILED on copy, live untouched: index 'idx_nope' references missing column users.ghost -- add it in 'columns' first
```

## Traps

- **`IF NOT EXISTS` on an index does not guard the column reference.** The
  column must exist first; ordering is the only fix.
- **`.bail on` (default for setup scripts) makes the first error fatal.** One
  mis-ordered statement silently skips every later migration in the file.
- **`cp` on a live DB can copy a torn page.** With WAL mode, use
  `VACUUM INTO 'copy.db'` or `sqlite3 db ".backup copy.db"` for a consistent
  snapshot instead of a raw file copy.
- **`ADD COLUMN ... DEFAULT <non-constant>` is rejected** by SQLite
  (e.g. `DEFAULT CURRENT_TIMESTAMP` is allowed, but a subquery is not). Keep
  defaults constant; backfill computed values with a follow-up `UPDATE`.
- **`ADD COLUMN ... NOT NULL` requires a default** on a non-empty table, or the
  ALTER fails. Add the column nullable, backfill, then enforce in app code.
- **Don't assume column names â€” read `PRAGMA table_info` every time.** Schema,
  casing, and order vary per DB; SQLite has type affinity, not strict types.
- **Fragile shell loops over table names break under quoting.** Generate the
  COUNT SQL with SQLite (the `gen`/`snapshot` pattern above) or use the Python
  helper, rather than nesting `sqlite3` inside a `while read` subshell.

## Verification Checklist

- [ ] Migration ran clean on a **copy** before the live file.
- [ ] Every `ADD COLUMN` precedes any index referencing that column.
- [ ] Re-running the migration changes nothing (idempotent).
- [ ] Per-table row counts identical before and after.
- [ ] `PRAGMA integrity_check` returns `ok`.
- [ ] Live DB backed up (`*.bak`) before `--apply`.
