# RT-01 Phase 2: Migration subsystem
# hermes_state_migration.py — Session lifecycle + resume state migration
# Implements RT01-B and RT01-C.

"""
RT-01 Phase 2: Unify session lifecycle and resume state into state.db.

This module provides the migration engine that:
1. Creates new tables (gateway_session_index, session_recovery_log, migration_state)
2. Adds new columns to sessions (migrated_to_state_db, resume_pending, resume_reason,
   last_resume_marked_at, suspended)
3. Imports sessions.json entries into gateway_session_index
4. Atomically renames sessions.json after successful import
5. Provides query/update methods for gateway session index and resume state

Migration is idempotent: if interrupted at any phase, a restart resumes from the
last recorded migration_state phase.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_state_backup import StateDBBackupManager

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Schema additions (appended to SCHEMA_SQL in hermes_state.py)
# ─────────────────────────────────────────────────────────────────────────────

MIGRATION_SCHEMA_SQL = """
-- RT01-B: Gateway session index — maps gateway session_key to session_id
CREATE TABLE IF NOT EXISTS gateway_session_index (
    session_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    platform TEXT,
    chat_type TEXT DEFAULT 'dm',
    display_name TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- RT01-C: Session recovery log — audit trail for resume events
CREATE TABLE IF NOT EXISTS session_recovery_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    session_id TEXT,
    event_type TEXT NOT NULL,
    reason TEXT,
    timestamp REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recovery_log_session_key
    ON session_recovery_log(session_key, timestamp DESC);

-- RT01-B/C: Migration state machine — tracks migration progress
CREATE TABLE IF NOT EXISTS migration_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- RT01-B/C: New columns on sessions table (idempotent via _reconcile_columns)
-- These are declared in SCHEMA_SQL so _reconcile_columns adds them automatically.
"""

# New columns to add to sessions table (declared in SCHEMA_SQL, auto-reconciled)
NEW_SESSION_COLUMNS = {
    "migrated_to_state_db": "INTEGER DEFAULT 0",
    "resume_pending": "INTEGER DEFAULT 0",
    "resume_reason": "TEXT",
    "last_resume_marked_at": "REAL",
    "suspended": "INTEGER DEFAULT 0",
}


class MigrationState:
    """Thin wrapper for the migration_state table."""

    PHASE_M0 = "M0"
    PHASE_M1 = "M1"
    PHASE_M2 = "M2"
    PHASE_M3 = "M3"
    PHASE_COMPLETE = "COMPLETE"

    @staticmethod
    def get_phase(conn: sqlite3.Connection) -> Optional[str]:
        try:
            row = conn.execute(
                "SELECT value FROM migration_state WHERE key = 'phase'"
            ).fetchone()
            return row[0] if row else None
        except sqlite3.OperationalError:
            return None

    @staticmethod
    def set_phase(conn: sqlite3.Connection, phase: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO migration_state (key, value, updated_at) "
            "VALUES ('phase', ?, ?)",
            (phase, time.time()),
        )
        conn.commit()

    @staticmethod
    def get_value(conn: sqlite3.Connection, key: str) -> Optional[str]:
        try:
            row = conn.execute(
                "SELECT value FROM migration_state WHERE key = ?",
                (key,),
            ).fetchone()
            return row[0] if row else None
        except sqlite3.OperationalError:
            return None

    @staticmethod
    def set_value(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO migration_state (key, value, updated_at) "
            "VALUES (?, ?, ?)",
            (key, value, time.time()),
        )
        conn.commit()

    @staticmethod
    def is_migration_complete(conn: sqlite3.Connection) -> bool:
        phase = MigrationState.get_phase(conn)
        return phase == MigrationState.PHASE_M3 or phase == MigrationState.PHASE_COMPLETE


class RT01MigrationEngine:
    """
    Drives the M0→M3 migration for RT01-B/C.

    Usage:
        engine = RT01MigrationEngine(db_path, sessions_json_path, conn)
        engine.run_migration()
    """

    def __init__(
        self,
        db_path: Path,
        sessions_json_path: Optional[Path],
        conn: sqlite3.Connection,
        backup_mgr: StateDBBackupManager,
    ):
        self.db_path = Path(db_path)
        self.sessions_json_path = sessions_json_path
        self._conn = conn
        self._backup_mgr = backup_mgr

    # ── Public API ──────────────────────────────────────────────────────────

    def run_migration(self) -> Dict[str, Any]:
        """
        Run the full migration M0→M3 if not already complete.

        Returns a report dict with phase, actions taken, and status.
        """
        current_phase = MigrationState.get_phase(self._conn)
        if current_phase == MigrationState.PHASE_COMPLETE:
            return {"phase": "COMPLETE", "status": "already_complete", "actions": []}

        # M3 means the transaction was committed and sessions.json was renamed.
        # But sessions.json may have been restored manually (crash recovery scenario),
        # so check if sessions.json still exists and re-run M2/M3 if needed.
        if current_phase == MigrationState.PHASE_M3:
            if self.sessions_json_path and self.sessions_json_path.exists():
                # sessions.json reappeared after M3 — re-import and re-cutover
                pass  # fall through to M2/M3
            else:
                return {"phase": "M3", "status": "already_complete", "actions": []}

        report = {"actions": [], "status": "in_progress"}

        # M0: pre-flight
        if not self._run_m0(report):
            report["status"] = "failed"
            report["phase"] = "M0"
            return report

        # M1: schema DDL
        if not self._run_m1(report):
            report["status"] = "failed"
            report["phase"] = "M1"
            return report

        # M2: data import
        if not self._run_m2(report):
            report["status"] = "failed"
            report["phase"] = "M2"
            return report

        # M3: commit and cutover
        if not self._run_m3(report):
            report["status"] = "failed"
            report["phase"] = "M3"
            return report

        report["status"] = "complete"
        report["phase"] = "M3"
        return report

    # ── M0: Pre-flight ─────────────────────────────────────────────────────

    def _run_m0(self, report: Dict[str, Any]) -> bool:
        phase = MigrationState.get_phase(self._conn)
        if phase and phase != MigrationState.PHASE_M0:
            # Already past M0
            return True

        try:
            # Integrity checks
            qc = self._conn.execute("PRAGMA quick_check").fetchone()
            if qc[0].lower() != "ok":
                report["actions"].append({"M0": "quick_check failed", "result": qc[0]})
                return False
            report["actions"].append({"M0": "quick_check passed"})

            # FK check
            fk_violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_violations:
                report["actions"].append({
                    "M0": "foreign_key_check failed",
                    "violations": len(fk_violations),
                })
                return False
            report["actions"].append({"M0": "foreign_key_check passed"})

            # Pre-upgrade snapshot
            snapshot = self._backup_mgr.create_pre_upgrade_snapshot()
            report["actions"].append({"M0": "pre_upgrade_snapshot", "path": snapshot.get("path")})

            # Acquire exclusive lock
            self._conn.execute("BEGIN IMMEDIATE")
            MigrationState.set_phase(self._conn, MigrationState.PHASE_M0)
            report["actions"].append({"M0": "phase set to M0"})

            return True
        except Exception as exc:
            logger.error("M0 failed: %s", exc)
            report["actions"].append({"M0": f"error: {exc}"})
            try:
                self._conn.rollback()
            except Exception:
                pass
            return False

    # ── M1: Schema DDL ─────────────────────────────────────────────────────

    def _run_m1(self, report: Dict[str, Any]) -> bool:
        phase = MigrationState.get_phase(self._conn)
        if phase and phase not in (MigrationState.PHASE_M0, None):
            # Already past M1
            return True

        try:
            # Create tables (idempotent)
            self._conn.executescript(MIGRATION_SCHEMA_SQL)

            # Verify tables exist
            for table in ("gateway_session_index", "session_recovery_log", "migration_state"):
                row = self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not row:
                    report["actions"].append({"M1": f"table {table} not created"})
                    return False

            MigrationState.set_phase(self._conn, MigrationState.PHASE_M1)
            report["actions"].append({"M1": "schema DDL complete"})
            return True
        except Exception as exc:
            logger.error("M1 failed: %s", exc)
            report["actions"].append({"M1": f"error: {exc}"})
            try:
                self._conn.rollback()
            except Exception:
                pass
            return False

    # ── M2: Data import ────────────────────────────────────────────────────

    def _run_m2(self, report: Dict[str, Any]) -> bool:
        phase = MigrationState.get_phase(self._conn)
        # M2 can run from M0, M1, M2 (re-run), M3 (re-import after crash), or None
        if phase and phase not in (MigrationState.PHASE_M0, MigrationState.PHASE_M1, MigrationState.PHASE_M2, MigrationState.PHASE_M3, None):
            # Already past M2 (only COMPLETE is past M2)
            return True

        if not self.sessions_json_path or not self.sessions_json_path.exists():
            # No sessions.json to import — skip M2, go straight to M3
            MigrationState.set_phase(self._conn, MigrationState.PHASE_M2)
            report["actions"].append({"M2": "no sessions.json found, skipping import"})
            return True

        try:
            with open(self.sessions_json_path, "r", encoding="utf-8") as f:
                sessions_data = json.load(f)

            imported = 0
            skipped = 0
            for session_key, entry_data in sessions_data.items():
                session_id = entry_data.get("session_id")
                if not session_id:
                    skipped += 1
                    continue

                created_at_str = entry_data.get("created_at")
                updated_at_str = entry_data.get("updated_at")
                created_at = self._parse_iso_timestamp(created_at_str) or time.time()
                updated_at = self._parse_iso_timestamp(updated_at_str) or time.time()

                platform = entry_data.get("platform")
                chat_type = entry_data.get("chat_type", "dm")
                display_name = entry_data.get("display_name")

                # Insert or ignore into sessions if not present
                self._conn.execute(
                    "INSERT OR IGNORE INTO sessions (id, source, started_at) "
                    "VALUES (?, ?, ?)",
                    (session_id, platform or "gateway", created_at),
                )

                # Insert or replace into gateway_session_index
                self._conn.execute(
                    "INSERT OR REPLACE INTO gateway_session_index "
                    "(session_key, session_id, created_at, updated_at, platform, chat_type, display_name) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (session_key, session_id, created_at, updated_at, platform, chat_type, display_name),
                )

                # Mark migrated (guard for older DBs that may not have the column yet)
                try:
                    self._conn.execute(
                        "UPDATE sessions SET migrated_to_state_db = 1 WHERE id = ?",
                        (session_id,),
                    )
                except sqlite3.OperationalError:
                    pass  # column doesn't exist yet — _reconcile_columns will add it
                imported += 1

            # Log resume state for entries that have resume_pending
            for session_key, entry_data in sessions_data.items():
                if entry_data.get("resume_pending"):
                    session_id = entry_data.get("session_id")
                    reason = entry_data.get("resume_reason", "restart_timeout")
                    lrma = self._parse_iso_timestamp(entry_data.get("last_resume_marked_at"))
                    try:
                        self._conn.execute(
                            "UPDATE sessions SET resume_pending = 1, "
                            "resume_reason = ?, last_resume_marked_at = ? "
                            "WHERE id = ?",
                            (reason, lrma, session_id),
                        )
                    except sqlite3.OperationalError:
                        pass  # columns don't exist yet
                    self._conn.execute(
                        "INSERT INTO session_recovery_log "
                        "(session_key, session_id, event_type, reason, timestamp) "
                        "VALUES (?, ?, 'resume_imported', ?, ?)",
                        (session_key, session_id, reason, time.time()),
                    )

                if entry_data.get("suspended"):
                    session_id = entry_data.get("session_id")
                    try:
                        self._conn.execute(
                            "UPDATE sessions SET suspended = 1 WHERE id = ?",
                            (session_id,),
                        )
                    except sqlite3.OperationalError:
                        pass  # column doesn't exist yet

            self._conn.commit()
            MigrationState.set_phase(self._conn, MigrationState.PHASE_M2)
            report["actions"].append({
                "M2": f"imported {imported} entries, skipped {skipped}",
            })
            return True
        except Exception as exc:
            logger.error("M2 failed: %s", exc)
            report["actions"].append({"M2": f"error: {exc}"})
            try:
                self._conn.rollback()
            except Exception:
                pass
            return False

    # ── M3: Commit and cutover ─────────────────────────────────────────────

    def _run_m3(self, report: Dict[str, Any]) -> bool:
        phase = MigrationState.get_phase(self._conn)
        if phase == MigrationState.PHASE_M3:
            return True

        try:
            # Commit transaction
            self._conn.commit()
            report["actions"].append({"M3": "transaction committed"})

            # Rename sessions.json
            if self.sessions_json_path and self.sessions_json_path.exists():
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                renamed = self.sessions_json_path.with_suffix(
                    f".json.migrated-{timestamp}"
                )
                try:
                    os.replace(str(self.sessions_json_path), str(renamed))
                    report["actions"].append({"M3": f"sessions.json renamed to {renamed.name}"})
                except OSError as exc:
                    logger.warning("M3: could not rename sessions.json: %s", exc)
                    report["actions"].append({"M3": f"sessions.json rename failed: {exc}"})
            else:
                report["actions"].append({"M3": "no sessions.json to rename"})

            # Write migration report
            report_path = self.db_path.parent / "migration-reports" / f"rt01-v2-{int(time.time())}.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
            report["actions"].append({"M3": f"report written to {report_path}"})

            # Set phase to M3/COMPLETE
            MigrationState.set_phase(self._conn, MigrationState.PHASE_M3)
            MigrationState.set_value(self._conn, "completed_at", str(time.time()))
            report["actions"].append({"M3": "phase set to M3"})

            return True
        except Exception as exc:
            logger.error("M3 failed: %s", exc)
            report["actions"].append({"M3": f"error: {exc}"})
            try:
                self._conn.rollback()
            except Exception:
                pass
            return False

    # ── Gateway session index methods ──────────────────────────────────────

    def get_gateway_session(self, session_key: str) -> Optional[Dict[str, Any]]:
        """Look up a gateway session by session_key."""
        row = self._conn.execute(
            "SELECT session_key, session_id, created_at, updated_at, platform, chat_type, display_name "
            "FROM gateway_session_index WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        if not row:
            return None
        return {
            "session_key": row[0],
            "session_id": row[1],
            "created_at": row[2],
            "updated_at": row[3],
            "platform": row[4],
            "chat_type": row[5],
            "display_name": row[6],
        }

    def upsert_gateway_session(
        self,
        session_key: str,
        session_id: str,
        created_at: float,
        updated_at: float,
        platform: Optional[str] = None,
        chat_type: str = "dm",
        display_name: Optional[str] = None,
    ) -> None:
        """Insert or replace a gateway session index entry.

        Ensures the referenced sessions row exists first to satisfy the
        foreign key constraint even when callers create the index mapping
        before the canonical session row.
        """
        # FK guard: ensure sessions row exists before referencing it.
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions (id, source, started_at) "
                "VALUES (?, ?, ?)",
                (session_id, platform or "gateway", created_at),
            )
        except sqlite3.OperationalError:
            # sessions table may be missing during very early schema init;
            # let the FK check surface any real issue
            pass

        self._conn.execute(
            "INSERT OR REPLACE INTO gateway_session_index "
            "(session_key, session_id, created_at, updated_at, platform, chat_type, display_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_key, session_id, created_at, updated_at, platform, chat_type, display_name),
        )
        self._conn.commit()

    def delete_gateway_session(self, session_key: str) -> bool:
        """Delete a gateway session index entry."""
        cur = self._conn.execute(
            "DELETE FROM gateway_session_index WHERE session_key = ?",
            (session_key,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_gateway_sessions(self) -> List[Dict[str, Any]]:
        """List all gateway session index entries."""
        rows = self._conn.execute(
            "SELECT session_key, session_id, created_at, updated_at, platform, chat_type, display_name "
            "FROM gateway_session_index ORDER BY updated_at DESC"
        ).fetchall()
        return [
            {
                "session_key": row[0],
                "session_id": row[1],
                "created_at": row[2],
                "updated_at": row[3],
                "platform": row[4],
                "chat_type": row[5],
                "display_name": row[6],
            }
            for row in rows
        ]

    # ── Resume state methods (RT01-C) ──────────────────────────────────────

    def get_resume_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get resume state for a session from the sessions table."""
        row = self._conn.execute(
            "SELECT resume_pending, resume_reason, last_resume_marked_at, suspended "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "resume_pending": bool(row[0]),
            "resume_reason": row[1],
            "last_resume_marked_at": row[2],
            "suspended": bool(row[3]),
        }

    def set_resume_pending(
        self, session_id: str, reason: str = "restart_timeout"
    ) -> bool:
        """Mark a session as resume_pending."""
        cur = self._conn.execute(
            "UPDATE sessions SET resume_pending = 1, resume_reason = ?, "
            "last_resume_marked_at = ? WHERE id = ?",
            (reason, time.time(), session_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def clear_resume_pending(self, session_id: str) -> bool:
        """Clear resume_pending for a session."""
        cur = self._conn.execute(
            "UPDATE sessions SET resume_pending = 0, resume_reason = NULL, "
            "last_resume_marked_at = NULL WHERE id = ?",
            (session_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def set_suspended(self, session_id: str) -> bool:
        """Mark a session as suspended."""
        cur = self._conn.execute(
            "UPDATE sessions SET suspended = 1 WHERE id = ?",
            (session_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def log_recovery_event(
        self,
        session_key: str,
        session_id: Optional[str],
        event_type: str,
        reason: Optional[str] = None,
    ) -> None:
        """Log a recovery event to session_recovery_log."""
        self._conn.execute(
            "INSERT INTO session_recovery_log "
            "(session_key, session_id, event_type, reason, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_key, session_id, event_type, reason, time.time()),
        )
        self._conn.commit()

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_iso_timestamp(ts_str: Optional[str]) -> Optional[float]:
        if not ts_str:
            return None
        try:
            dt = datetime.fromisoformat(ts_str)
            return dt.timestamp()
        except (ValueError, TypeError):
            return None

# ─────────────────────────────────────────────────────────────────────────────
# RT-02: Compression lineage migration helpers
# ─────────────────────────────────────────────────────────────────────────────

class CompressionLineageMigration:
    """Backfill compression_lineage records from sessions.parent_session_id.

    When upgrading from v16 to v17, existing compressed sessions already have
    a parent_session_id in the sessions table but no entry in compression_lineage.
    This helper creates lineage records for those sessions idempotently.
    """

    @staticmethod
    def run(conn: sqlite3.Connection) -> int:
        """Create lineage rows for existing parent-child relationships.

        Returns the number of lineage records inserted.
        """
        # Find sessions that have a parent_session_id but no lineage entry.
        rows = conn.execute(
            "SELECT s.id, s.parent_session_id, s.started_at, s.title "
            "FROM sessions s "
            "WHERE s.parent_session_id IS NOT NULL "
            "AND s.id NOT IN (SELECT child_session_id FROM compression_lineage)"
        ).fetchall()
        now = time.time()
        inserted = 0
        for row in rows:
            child_id = row[0]
            parent_id = row[1]
            started_at = row[2]
            title = row[3] or ""
            # Heuristic: trigger from title keywords if possible.
            trigger = "MIGRATION_BACKFILL"
            if "compress" in title.lower():
                trigger = "AUTO_THRESHOLD"
            elif "summar" in title.lower():
                trigger = "AUTO_THRESHOLD"
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO compression_lineage "
                    "(child_session_id, parent_session_id, strategy, trigger, "
                    "tokens_before, tokens_after, content_hash, rollback_token, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        child_id, parent_id, "unknown", trigger,
                        0, 0, "migration", f"migration-{child_id}",
                        started_at or now,
                    ),
                )
                if conn.total_changes:
                    inserted += 1
            except sqlite3.Error as exc:
                logger.warning("Failed to backfill lineage for %s: %s", child_id, exc)
        conn.commit()
        return inserted

    @staticmethod
    def rebuild_lineage_versions(conn: sqlite3.Connection) -> int:
        """Initialize sessions.lineage_version for existing sessions.

        Each session gets version 0; it will be incremented on the next
        compression/lineage change.
        """
        cur = conn.execute(
            "UPDATE sessions SET lineage_version = COALESCE(lineage_version, 0) "
            "WHERE lineage_version IS NULL"
        )
        conn.commit()
        return cur.rowcount

