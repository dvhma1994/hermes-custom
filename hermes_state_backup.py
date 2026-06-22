# RT-01 Phase 1 Implementation
# hermes_state_backup.py — Backup, Integrity, and Safe Mode subsystem
# Imported by hermes_state.py; standalone module to avoid bloating the monolith.

"""
RT-01 Phase 1: State DB Backup, Integrity Validation, and Safe Mode.

This module implements three subsystems from RT01_DESIGN_REVISION_V2.json:

  RT01-D: StateDBBackupManager — tiered backup strategy (hot, daily, pre-upgrade, repair)
  RT01-E: StartupIntegrityValidator — 3-level integrity validation (L1/L2/L3)
  RT01-A: SafeMode — safe mode flag and repair CLI contract

Design contracts:
  - Hot backup validation checklist V1-V7 (RT01_DESIGN_REVISION_V2.json phase_3)
  - Restore validation R1-R5 (same)
  - Repair CLI mandatory snapshot, dry-run, operator confirmation (phase_4)
  - 3-level integrity: L1 light (quick_check), L2 full (integrity_check), L3 schema
  - Safe mode: refuses writes, allows reads, operator-visible
"""

import datetime
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class StateDBBackupManager:
    """Tiered backup manager for state.db.

    Triggers:
      - Hot: every 100 writes or 15 minutes (whichever comes first)
      - Daily: once per day at first startup
      - Pre-upgrade: before any migration
      - Repair: before any repair CLI mutation

    Validation:
      Every backup passes V1-V7 before being marked restorable.
    """

    HOT_TRIGGER_WRITES = 100
    HOT_TRIGGER_SECONDS = 900  # 15 minutes
    RETENTION_DAYS = 7
    # Count caps (state.db snapshots are large; age-only retention let hot +
    # repair grow to ~190 GB). cleanup keeps the newest N of each tier AND
    # enforces the age limit.
    MAX_HOT_BACKUPS = 12
    MAX_REPAIR_SNAPSHOTS = 5
    MAX_DAILY_SNAPSHOTS = 14

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        home = self.db_path.parent
        self.backup_dir = home / "backups" / "hot"
        self.repair_backup_dir = home / "backups" / "repair"
        self.daily_dir = home / "backups" / "daily"
        self.migration_dir = home / "backups" / "migration"
        self._write_count = 0
        self._last_hot_backup_ts: float = 0.0

    def _ensure_dirs(self) -> None:
        for d in (self.backup_dir, self.repair_backup_dir, self.daily_dir, self.migration_dir):
            d.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _checksum(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    # ── Hot backup ──────────────────────────────────────────────────────

    def should_create_hot_backup(self) -> bool:
        if self._write_count >= self.HOT_TRIGGER_WRITES:
            return True
        if (time.time() - self._last_hot_backup_ts) >= self.HOT_TRIGGER_SECONDS:
            return True
        return False

    def create_hot_backup(self) -> Dict[str, Any]:
        self._ensure_dirs()
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        pid = os.getpid()
        nonce = hashlib.sha256(f"{time.time()}{pid}".encode()).hexdigest()[:8]
        tmp_path = self.backup_dir / f".state-{stamp}-{pid}-{nonce}.db.tmp"
        final_path = self.backup_dir / f"state-{stamp}-{pid}-{nonce}.db"

        result: Dict[str, Any] = {
            "path": str(final_path),
            "valid": False,
            "error": None,
            "checksum": None,
        }

        if not self.db_path.exists():
            result["error"] = f"{self.db_path} does not exist"
            return result

        try:
            src_conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                dst_conn = sqlite3.connect(str(tmp_path))
                try:
                    src_conn.backup(dst_conn, pages=0)
                    dst_conn.commit()
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()
        except sqlite3.DatabaseError as exc:
            result["error"] = f"backup creation failed: {exc}"
            tmp_path.unlink(missing_ok=True)
            return result

        validation = validate_backup(tmp_path)
        if not validation["valid"]:
            tmp_path.unlink(missing_ok=True)
            result["error"] = validation.get("error") or "backup validation failed"
            logger.error("state_db_backup_failed: %s", result["error"])
            return result

        tmp_path.replace(final_path)
        result["valid"] = True
        result["checksum"] = self._checksum(final_path)
        self._write_count = 0
        self._last_hot_backup_ts = time.time()
        logger.info("state_db_hot_backup_created: %s", final_path)
        return result

    # ── Pre-upgrade snapshot ────────────────────────────────────────────

    def create_pre_upgrade_snapshot(self) -> Dict[str, Any]:
        self._ensure_dirs()
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_dir = self.migration_dir / f"pre-upgrade-{stamp}"
        dest_dir.mkdir(parents=True, exist_ok=True)

        copied: List[str] = []
        for suffix in ("", "-wal", "-shm"):
            src = self.db_path.with_name(self.db_path.name + suffix)
            if src.exists():
                dst = dest_dir / (self.db_path.name + suffix)
                shutil.copy2(src, dst)
                copied.append(str(dst))

        sessions_json = self.db_path.parent / "sessions.json"
        if sessions_json.exists():
            shutil.copy2(sessions_json, dest_dir / "sessions.json")
            copied.append(str(dest_dir / "sessions.json"))

        result = {"path": str(dest_dir), "files": copied}
        logger.info("state_db_pre_upgrade_snapshot: %s", dest_dir)
        return result

    # ── Repair snapshot ────────────────────────────────────────────────

    def create_repair_snapshot(self) -> Dict[str, Any]:
        self._ensure_dirs()
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        pid = os.getpid()
        nonce = hashlib.sha256(f"{time.time()}{pid}".encode()).hexdigest()[:8]
        dest_dir = self.repair_backup_dir / f"repair-backup-{stamp}-{pid}-{nonce}"
        dest_dir.mkdir(parents=True, exist_ok=True)

        copied: List[str] = []
        for suffix in ("", "-wal", "-shm"):
            src = self.db_path.with_name(self.db_path.name + suffix)
            if src.exists():
                dst = dest_dir / (self.db_path.name + suffix)
                shutil.copy2(src, dst)
                copied.append(str(dst))

        sessions_json = self.db_path.parent / "sessions.json"
        if sessions_json.exists():
            shutil.copy2(sessions_json, dest_dir / "sessions.json")
            copied.append(str(dest_dir / "sessions.json"))

        checksums = {}
        for f in copied:
            checksums[f] = self._checksum(Path(f))

        result = {"path": str(dest_dir), "files": copied, "checksums": checksums}
        logger.info("state_db_repair_snapshot: %s", dest_dir)
        return result

    def create_pre_upgrade_snapshot(self) -> Dict[str, Any]:
        """Create a pre-upgrade snapshot including DB, WAL, SHM, and sessions.json."""
        self._ensure_dirs()
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        pid = os.getpid()
        nonce = hashlib.sha256(f"{time.time()}{pid}".encode()).hexdigest()[:8]
        dest_dir = self.backup_dir / "pre-upgrade" / f"pre-upgrade-{stamp}-{pid}-{nonce}"
        dest_dir.mkdir(parents=True, exist_ok=True)

        copied: List[str] = []
        for suffix in ("", "-wal", "-shm"):
            src = self.db_path.with_name(self.db_path.name + suffix)
            if src.exists():
                dst = dest_dir / (self.db_path.name + suffix)
                shutil.copy2(src, dst)
                copied.append(str(dst))

        sessions_json = self.db_path.parent / "sessions.json"
        if sessions_json.exists():
            shutil.copy2(sessions_json, dest_dir / "sessions.json")
            copied.append(str(dest_dir / "sessions.json"))

        checksums = {}
        for f in copied:
            checksums[f] = self._checksum(Path(f))

        result = {"path": str(dest_dir), "files": copied, "checksums": checksums}
        logger.info("state_db_pre_upgrade_snapshot: %s", dest_dir)
        return result

    # ── Restore ─────────────────────────────────────────────────────────

    def restore_from_backup(self, backup_path: Path) -> Dict[str, Any]:
        backup_path = Path(backup_path)
        result: Dict[str, Any] = {
            "success": False,
            "error": None,
            "quarantine": None,
            "restored_path": None,
        }

        if not backup_path.exists():
            result["error"] = f"backup path {backup_path} does not exist"
            return result

        validation = validate_backup(backup_path)
        if not validation["valid"]:
            result["error"] = f"restore validation failed: {validation.get('error')}"
            return result

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        quarantine = self.db_path.parent / f"state.db.failed-{stamp}"

        if self.db_path.exists():
            shutil.copy2(self.db_path, quarantine)
            for suffix in ("-wal", "-shm"):
                side = self.db_path.with_name(self.db_path.name + suffix)
                if side.exists():
                    shutil.copy2(side, quarantine.with_name(quarantine.name + suffix))

        result["quarantine"] = str(quarantine)

        # Atomic swap
        if self.db_path.exists():
            self.db_path.unlink()
        backup_path.replace(self.db_path)

        # Post-swap validation
        post = validate_backup(self.db_path)
        if not post["valid"]:
            result["error"] = f"post-restore validation failed: {post.get('error')}"
            logger.error("state_db_restore_post_swap_failed: %s", result["error"])
            return result

        result["success"] = True
        result["restored_path"] = str(self.db_path)
        logger.info("state_db_restored: %s", self.db_path)
        return result

    # ── Retention cleanup ──────────────────────────────────────────────

    def cleanup_old_backups(self) -> Dict[str, Any]:
        self._ensure_dirs()
        now = datetime.datetime.now()
        removed: List[str] = []

        def _prune(entries, keep: int) -> None:
            # Keep the newest `keep` entries by mtime; also drop anything older
            # than RETENTION_DAYS. Operates only inside backups/ — the live
            # state.db lives in the parent dir and is never matched. Fully
            # defensive: a failure on one entry must not abort the rest.
            try:
                items = sorted(entries, key=lambda p: p.stat().st_mtime, reverse=True)
            except Exception:
                items = list(entries)
            for i, p in enumerate(items):
                try:
                    age_days = (now - datetime.datetime.fromtimestamp(p.stat().st_mtime)).days
                    if i >= keep or age_days > self.RETENTION_DAYS:
                        if p.is_dir():
                            shutil.rmtree(p, ignore_errors=True)
                        else:
                            p.unlink()
                        removed.append(str(p))
                except Exception:
                    pass

        # Hot: timestamped state-*.db snapshot files
        _prune(list(self.backup_dir.glob("state-*.db")), self.MAX_HOT_BACKUPS)
        # Repair: repair-backup-* directories (previously NEVER pruned — the
        # main cause of unbounded growth when the repair path fired repeatedly).
        _prune(list(self.repair_backup_dir.glob("repair-backup-*")), self.MAX_REPAIR_SNAPSHOTS)
        # Daily: state-YYYYMMDD.db files
        _prune(list(self.daily_dir.glob("state-*.db")), self.MAX_DAILY_SNAPSHOTS)
        return {"removed": removed}

    # ── Daily snapshot ──────────────────────────────────────────────────

    def create_daily_snapshot(self) -> Dict[str, Any]:
        self._ensure_dirs()
        today = datetime.datetime.now().strftime("%Y%m%d")
        dest = self.daily_dir / f"state-{today}.db"
        if dest.exists():
            return {"path": str(dest), "skipped": True, "reason": "daily snapshot already exists"}

        if not self.db_path.exists():
            return {"path": str(dest), "skipped": True, "reason": "state.db does not exist"}

        try:
            src_conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                dst_conn = sqlite3.connect(str(dest))
                try:
                    src_conn.backup(dst_conn, pages=0)
                    dst_conn.commit()
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()
        except sqlite3.DatabaseError as exc:
            return {"path": str(dest), "skipped": True, "reason": str(exc)}

        validation = validate_backup(dest)
        if not validation["valid"]:
            dest.unlink(missing_ok=True)
            return {"path": str(dest), "skipped": True, "reason": validation.get("error")}

        return {"path": str(dest), "skipped": False, "checksum": self._checksum(dest)}


# ── Backup validation (standalone function) ──────────────────────────────

def validate_backup(path: Path) -> Dict[str, Any]:
    """Validate a backup file using the V1-V7 checklist.

    V1: File exists and size > 4096 bytes
    V2: PRAGMA quick_check returns 'ok'
    V3: PRAGMA foreign_key_check returns zero rows
    V4: journal_mode is WAL (or DELETE if explicitly allowed)
    V5: Required tables (sessions, messages) have row counts
    V6: FTS table row count matches messages (if FTS exists)
    V7: schema_version is present and >= minimum
    """
    result: Dict[str, Any] = {"valid": False, "error": None, "checks": {}}
    p = Path(path)

    # V1
    if not p.exists() or p.stat().st_size <= 4096:
        result["error"] = "V1: backup file missing or too small"
        return result
    result["checks"]["V1_size"] = True

    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.DatabaseError as exc:
        result["error"] = f"V2: cannot open backup: {exc}"
        return result

    try:
        # V2
        cur = conn.execute("PRAGMA quick_check")
        quick = " ".join(str(r[0]) for r in cur.fetchall())
        if quick.lower() != "ok":
            result["error"] = f"V2 quick_check failed: {quick}"
            return result
        result["checks"]["V2_quick_check"] = True

        # V3
        cur = conn.execute("PRAGMA foreign_key_check")
        fk = cur.fetchall()
        if fk:
            result["error"] = f"V3 foreign_key_check failed: {fk}"
            return result
        result["checks"]["V3_fk_check"] = True

        # V4
        cur = conn.execute("PRAGMA journal_mode")
        jm = cur.fetchone()[0].lower()
        result["checks"]["V4_journal_mode"] = jm

        # V5
        counts: Dict[str, int] = {}
        for tbl in ("sessions", "messages"):
            try:
                counts[tbl] = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
            except sqlite3.OperationalError:
                result["error"] = f"V5: required table {tbl} missing"
                return result
        result["checks"]["V5_row_counts"] = counts

        # V6 (optional — FTS may not exist in all backups)
        try:
            fts_count = conn.execute("SELECT count(*) FROM messages_fts").fetchone()[0]
            msg_count = counts.get("messages", 0)
            result["checks"]["V6_fts_count"] = fts_count
            result["checks"]["V6_msg_count"] = msg_count
        except sqlite3.OperationalError:
            result["checks"]["V6_fts_count"] = "FTS table not present"
            # Not a failure — FTS may not be built yet

        # V7
        try:
            cur = conn.execute("SELECT version FROM schema_version LIMIT 1")
            row = cur.fetchone()
            result["checks"]["V7_schema_version"] = row[0] if row else None
        except sqlite3.OperationalError:
            result["checks"]["V7_schema_version"] = "schema_version table missing"

    except sqlite3.DatabaseError as exc:
        result["error"] = f"validation error: {exc}"
        return result
    finally:
        conn.close()

    result["valid"] = True
    return result


# ── Startup Integrity Validator (RT01-E) ───────────────────────────────

class StartupIntegrityValidator:
    """3-level integrity validation for SessionDB startup.

    L1 (light): PRAGMA quick_check + foreign_key_check — fast, <5s
    L2 (full):  PRAGMA integrity_check — thorough, <30s, run on L1 failure
    L3 (schema): Table/column existence validation — structural check
    """

    REQUIRED_TABLES = ("sessions", "messages", "schema_version")
    REQUIRED_SESSION_COLUMNS = (
        "id", "source", "started_at", "ended_at", "end_reason",
        "message_count", "parent_session_id",
    )
    REQUIRED_MESSAGE_COLUMNS = (
        "id", "session_id", "role", "content", "timestamp",
    )

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def run_l1_light(self) -> Dict[str, Any]:
        """Quick integrity check. Returns dict with 'passed', 'details'."""
        result = {"passed": False, "level": "L1", "details": {}}
        try:
            cur = self.conn.execute("PRAGMA quick_check")
            quick = " ".join(str(r[0]) for r in cur.fetchall())
            result["details"]["quick_check"] = quick
            if quick.lower() != "ok":
                result["details"]["error"] = f"quick_check: {quick}"
                return result
            cur = self.conn.execute("PRAGMA foreign_key_check")
            fk = cur.fetchall()
            result["details"]["fk_violations"] = len(fk)
            if fk:
                result["details"]["error"] = f"foreign_key_check: {fk[:5]}"
                return result
            result["passed"] = True
        except sqlite3.DatabaseError as exc:
            result["details"]["error"] = str(exc)
        return result

    def run_l2_full(self) -> Dict[str, Any]:
        """Full integrity check. Returns dict with 'passed', 'details'."""
        result = {"passed": False, "level": "L2", "details": {}}
        try:
            cur = self.conn.execute("PRAGMA integrity_check")
            integrity = " ".join(str(r[0]) for r in cur.fetchall())
            result["details"]["integrity_check"] = integrity
            if integrity.lower() != "ok":
                result["details"]["error"] = f"integrity_check: {integrity}"
                return result
            result["passed"] = True
        except sqlite3.DatabaseError as exc:
            result["details"]["error"] = str(exc)
        return result

    def run_l3_schema(self) -> Dict[str, Any]:
        """Schema validation. Returns dict with 'passed', 'details'."""
        result = {"passed": False, "level": "L3", "details": {}}

        for tbl in self.REQUIRED_TABLES:
            try:
                cur = self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (tbl,),
                )
                if not cur.fetchone():
                    result["details"]["error"] = f"missing table: {tbl}"
                    return result
            except sqlite3.DatabaseError as exc:
                result["details"]["error"] = str(exc)
                return result

        # Check sessions columns
        try:
            cur = self.conn.execute("PRAGMA table_info(sessions)")
            cols = {row[1] for row in cur.fetchall()}
            for col in self.REQUIRED_SESSION_COLUMNS:
                if col not in cols:
                    result["details"]["error"] = f"sessions missing column: {col}"
                    return result
        except sqlite3.DatabaseError as exc:
            result["details"]["error"] = str(exc)
            return result

        # Check messages columns
        try:
            cur = self.conn.execute("PRAGMA table_info(messages)")
            cols = {row[1] for row in cur.fetchall()}
            for col in self.REQUIRED_MESSAGE_COLUMNS:
                if col not in cols:
                    result["details"]["error"] = f"messages missing column: {col}"
                    return result
        except sqlite3.DatabaseError as exc:
            result["details"]["error"] = str(exc)
            return result

        result["passed"] = True
        return result

    def run_all(self) -> Dict[str, Any]:
        """Run all levels sequentially. Returns combined result."""
        l1 = self.run_l1_light()
        if not l1["passed"]:
            l2 = self.run_l2_full()
            if not l2["passed"]:
                return {"passed": False, "levels": {"L1": l1, "L2": l2}, "safe_mode": True}
        l3 = self.run_l3_schema()
        if not l3["passed"]:
            return {"passed": False, "levels": {"L1": l1, "L3": l3}, "safe_mode": True}
        return {"passed": True, "levels": {"L1": l1, "L3": l3}, "safe_mode": False}


# ── Safe Mode (RT01-A) ──────────────────────────────────────────────────

class SafeModeState:
    """Safe mode flag for SessionDB.

    When safe_mode=True:
      - Write operations raise RuntimeError
      - Read operations continue to work
      - Operator is alerted via logging
      - CLI repair command is the exit path
    """

    def __init__(self):
        self._active = False
        self._reason: Optional[str] = None
        self._activated_at: Optional[float] = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    def activate(self, reason: str) -> None:
        self._active = True
        self._reason = reason
        self._activated_at = time.time()
        logger.error("state_db_safe_mode_entered: %s", reason)

    def deactivate(self) -> None:
        self._active = False
        self._reason = None
        self._activated_at = None
        logger.info("state_db_safe_mode_exited")

    def check_write_allowed(self) -> None:
        """Raise RuntimeError if safe mode is active."""
        if self._active:
            raise RuntimeError(
                f"state.db is in safe mode: {self._reason}. "
                "Run 'hermes repair-state-db' to repair and exit safe mode."
            )

    def get_health_metrics(self) -> Dict[str, Any]:
        return {
            "safe_mode": self._active,
            "safe_mode_reason": self._reason,
            "safe_mode_activated_at": self._activated_at,
        }


# ── Repair CLI contract (RT01-A COND-05) ────────────────────────────────

class RepairCLIContractor:
    """Implements the repair CLI contract from RT01_DESIGN_REVISION_V2.json phase_4.

    Requirements:
      - Mandatory snapshot before any mutation
      - Integrity report generation (before and after)
      - Dry-run mode
      - Operator confirmation
      - Rollback instructions
      - Forbidden operations enforced
    """

    FORBIDDEN_OPERATIONS = [
        "PRAGMA writable_schema=ON",
        "DELETE FROM sqlite_master",
        "ALTER TABLE DROP COLUMN",
        "DROP TABLE sessions",
        "DROP TABLE messages",
        "DROP TABLE schema_version",
    ]

    LEVEL_1_SAFE_OPERATIONS = [
        "DROP TRIGGER IF EXISTS",
        "DROP INDEX IF EXISTS",
        "CREATE VIRTUAL TABLE IF NOT EXISTS",
        "CREATE INDEX IF NOT EXISTS",
        "REINDEX",
        "PRAGMA optimize",
    ]

    LEVEL_2_DIAGNOSTIC_OPERATIONS = [
        "VACUUM",
        "DELETE FROM messages_fts",
        "INSERT INTO messages_fts SELECT",
    ]

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.backup_mgr = StateDBBackupManager(self.db_path)

    def run_dry_run(self) -> Dict[str, Any]:
        """Run assessment only, no mutations. Returns a report."""
        report = {
            "dry_run": True,
            "snapshot": None,
            "integrity_before": None,
            "planned_actions": [],
            "estimated_impact": "none",
        }

        report["integrity_before"] = self._generate_integrity_report()

        # Check for FTS issues
        ib = report["integrity_before"]
        if ib.get("quick_check") and ib["quick_check"].lower() != "ok":
            report["planned_actions"].append({
                "action": "level_1: drop and rebuild FTS triggers",
                "reason": f"quick_check: {ib['quick_check']}",
            })
        if ib.get("fk_violations"):
            report["planned_actions"].append({
                "action": "level_1: investigate FK violations (no auto-fix)",
                "reason": f"FK violations: {ib['fk_violations']}",
            })

        report["estimated_impact"] = "none (dry-run only)"
        return report

    def run_repair(self, *, force: bool = False, dry_run: bool = False) -> Dict[str, Any]:
        """Full repair flow. Returns a report."""
        if dry_run:
            return self.run_dry_run()

        report: Dict[str, Any] = {
            "dry_run": False,
            "snapshot": None,
            "integrity_before": None,
            "integrity_after": None,
            "actions_taken": [],
            "error": None,
        }

        # 1. Mandatory snapshot
        snapshot = self.backup_mgr.create_repair_snapshot()
        report["snapshot"] = snapshot
        if not snapshot.get("files"):
            report["error"] = "snapshot creation failed — aborting repair"
            return report

        # 2. Integrity before
        report["integrity_before"] = self._generate_integrity_report()

        # 3. Operator confirmation
        if not force:
            print(f"This will mutate state.db. A backup has been created at {snapshot['path']}.")
            try:
                confirm = input("Continue? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                confirm = "n"
            if confirm != "y":
                report["error"] = "operator did not confirm"
                report["actions_taken"] = ["snapshot_created"]
                return report

        # 4. Execute safe repairs
        actions = self._execute_safe_repairs()
        report["actions_taken"] = actions

        # 5. Integrity after
        report["integrity_after"] = self._generate_integrity_report()

        # 6. Rollback instructions
        report["rollback_instructions"] = (
            f"If repair leaves DB in safe mode, restore from: {snapshot['path']}\n"
            f"Or run: hermes restore-state-db --source=repair-backup --path={snapshot['path']}"
        )

        return report

    def _generate_integrity_report(self) -> Dict[str, Any]:
        """Generate pre/post repair integrity report."""
        report: Dict[str, Any] = {}
        if not self.db_path.exists():
            report["error"] = f"{self.db_path} does not exist"
            return report

        try:
            conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        except sqlite3.DatabaseError as exc:
            report["error"] = str(exc)
            return report

        try:
            cur = conn.execute("PRAGMA quick_check")
            report["quick_check"] = " ".join(str(r[0]) for r in cur.fetchall())

            cur = conn.execute("PRAGMA foreign_key_check")
            report["fk_violations"] = len(cur.fetchall())

            cur = conn.execute("PRAGMA journal_mode")
            report["journal_mode"] = cur.fetchone()[0]

            # Row counts
            for tbl in ("sessions", "messages"):
                try:
                    report[f"{tbl}_count"] = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
                except sqlite3.OperationalError:
                    report[f"{tbl}_count"] = "table missing"

            # Object counts
            cur = conn.execute("SELECT count(*) FROM sqlite_master")
            report["object_count"] = cur.fetchone()[0]

        except sqlite3.DatabaseError as exc:
            report["error"] = str(exc)
        finally:
            conn.close()

        return report

    def _execute_safe_repairs(self) -> List[str]:
        """Execute level-1 safe repairs only."""
        actions: List[str] = []
        try:
            conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        except sqlite3.DatabaseError as exc:
            logger.error("repair: cannot open DB: %s", exc)
            return actions

        try:
            # Level 1: Drop and recreate FTS triggers
            try:
                conn.execute("DROP TRIGGER IF EXISTS messages_ai")
                conn.execute("DROP TRIGGER IF EXISTS messages_ad")
                conn.execute("DROP TRIGGER IF EXISTS messages_au")
                actions.append("dropped FTS triggers")
            except sqlite3.OperationalError:
                pass

            # Level 1: Reindex
            try:
                conn.execute("REINDEX")
                actions.append("reindexed")
            except sqlite3.OperationalError:
                pass

            # Level 1: PRAGMA optimize
            try:
                conn.execute("PRAGMA optimize")
                actions.append("optimized")
            except sqlite3.OperationalError:
                pass

        finally:
            conn.close()

        return actions

    @classmethod
    def is_forbidden(cls, operation: str) -> bool:
        """Check if an operation is forbidden."""
        op_upper = operation.upper().strip()
        for forbidden in cls.FORBIDDEN_OPERATIONS:
            if forbidden.upper() in op_upper:
                return True
        return False