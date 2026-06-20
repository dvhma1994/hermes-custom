"""OPVAL SQLite schema and persistence layer.

Implements the evidence storage schema from evidence_schema.json.
"""

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
import json
import sqlite3
import time
from pathlib import Path


# SQLite pragmas for safe concurrent access with the main Hermes SessionDB.
# Hermes state.db is already in WAL mode (verified); these pragmas are a no-op
# when WAL is already enabled and a safety net when it is not.
_OPVAL_PRAGMAS = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
]


OPVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS opval_sessions (
    session_id TEXT PRIMARY KEY,
    task_id TEXT,
    parent_session_id TEXT,
    root_session_id TEXT,
    platform TEXT,
    primary_domain TEXT,
    secondary_domains TEXT,
    start_time REAL,
    end_time REAL,
    turn_count INTEGER,
    tool_execution_count INTEGER DEFAULT 0,
    outcome TEXT,
    session_quality_score REAL,
    session_tool_correctness REAL,
    drift_pct REAL,
    misalignment_pct REAL,
    promotion_score REAL,
    synthetic INTEGER DEFAULT 0,
    synthetic_reason TEXT,
    opval_enabled INTEGER DEFAULT 1,
    recorded_at REAL
);

CREATE TABLE IF NOT EXISTS opval_turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT,
    turn_number INTEGER,
    outcome TEXT,
    tool_calls TEXT,
    tool_outputs TEXT,
    error_class TEXT,
    latency_ms INTEGER,
    tokens_used INTEGER,
    quality_score REAL,
    tool_execution_score REAL,
    drift_flag INTEGER,
    misalignment_flag INTEGER,
    checkpoint_event TEXT,
    started_at REAL,
    ended_at REAL,
    FOREIGN KEY (session_id) REFERENCES opval_sessions(session_id)
);

CREATE TABLE IF NOT EXISTS opval_readiness_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    computed_at REAL,
    gate_id TEXT,
    value REAL,
    pass INTEGER,
    window_start REAL,
    window_end REAL
);

CREATE TABLE IF NOT EXISTS opval_reports (
    report_id TEXT PRIMARY KEY,
    report_type TEXT,
    generated_at REAL,
    period_start REAL,
    period_end REAL,
    payload_json TEXT,
    delivered_to TEXT
);

-- Milestone 2 learning governance tables (created idempotently, no M3/M4 features)
CREATE TABLE IF NOT EXISTS learning_strategies (
    strategy_id TEXT PRIMARY KEY,
    strategy_text TEXT NOT NULL,
    status_cache_non_authoritative TEXT DEFAULT 'experimental'
        CHECK(status_cache_non_authoritative IN ('experimental','active','demoted','disabled')),
    source TEXT NOT NULL CHECK(source IN ('historical','generated')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_strategy_observations (
    observation_id TEXT PRIMARY KEY,
    opval_session_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL CHECK(evidence_type IN ('tool','delegate','compression','recovery')),
    outcome TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    source_module TEXT NOT NULL,
    owner TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    FOREIGN KEY (opval_session_id) REFERENCES opval_sessions(session_id),
    FOREIGN KEY (strategy_id) REFERENCES learning_strategies(strategy_id),
    UNIQUE(opval_session_id, strategy_id, evidence_type, source_hash)
);

CREATE TABLE IF NOT EXISTS learning_cases (
    case_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    directive_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    result_snapshot_json TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (observation_id) REFERENCES learning_strategy_observations(observation_id),
    FOREIGN KEY (strategy_id) REFERENCES learning_strategies(strategy_id),
    UNIQUE(observation_id, directive_json)
);

CREATE INDEX IF NOT EXISTS idx_opval_sessions_domain ON opval_sessions(primary_domain, start_time);
CREATE INDEX IF NOT EXISTS idx_opval_sessions_start_time ON opval_sessions(start_time);
CREATE INDEX IF NOT EXISTS idx_opval_sessions_root ON opval_sessions(root_session_id);
CREATE INDEX IF NOT EXISTS idx_opval_turns_session ON opval_turns(session_id, turn_number);
CREATE INDEX IF NOT EXISTS idx_opval_readiness_time ON opval_readiness_snapshots(computed_at, gate_id);
CREATE INDEX IF NOT EXISTS idx_learning_observations_session ON learning_strategy_observations(opval_session_id);
CREATE INDEX IF NOT EXISTS idx_learning_observations_strategy ON learning_strategy_observations(strategy_id);
CREATE INDEX IF NOT EXISTS idx_learning_cases_observation ON learning_cases(observation_id);

-- Milestone 3a: demotion recommendation queue (recommendation-only, no enforcement)
CREATE TABLE IF NOT EXISTS learning_demotion_recommendations (
    rec_id TEXT PRIMARY KEY,
    monitor_type TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL,
    processed_at REAL DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_demotion_rec_strategy ON learning_demotion_recommendations(strategy_id);
CREATE INDEX IF NOT EXISTS idx_demotion_rec_processed ON learning_demotion_recommendations(processed_at);

-- Milestone 3b: governance event chain (append-only)
CREATE TABLE IF NOT EXISTS learning_governance_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    decision_reason TEXT NOT NULL,
    previous_hash TEXT DEFAULT NULL,
    source_hash TEXT NOT NULL,
    effectiveness_result_json TEXT,
    authority_directive_json TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    owner TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_governance_events_strategy ON learning_governance_events(strategy_id, created_at);
CREATE INDEX IF NOT EXISTS idx_governance_events_type ON learning_governance_events(event_type, created_at);

-- Milestone 3b: drift snapshots
CREATE TABLE IF NOT EXISTS learning_drift_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    drift_pct REAL NOT NULL,
    misalignment_pct REAL NOT NULL,
    win_rate REAL NOT NULL,
    avg_score REAL NOT NULL,
    avg_alignment REAL NOT NULL,
    sample_count INTEGER NOT NULL,
    threshold_pct REAL NOT NULL,
    owner TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drift_strategy_created ON learning_drift_snapshots(strategy_id, created_at);

-- Milestone 3b: dataset batch metadata
CREATE TABLE IF NOT EXISTS learning_dataset_batches (
    batch_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    observation_count INTEGER NOT NULL,
    case_count INTEGER NOT NULL,
    batch_status TEXT NOT NULL,
    owner TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dataset_strategy ON learning_dataset_batches(strategy_id, created_at);

-- Milestone 3b: circuit breaker state
CREATE TABLE IF NOT EXISTS learning_mirror_circuit_breaker (
    breaker_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    activated_at REAL,
    expires_at REAL,
    reason TEXT NOT NULL,
    governance_events_count INTEGER NOT NULL DEFAULT 0,
    previous_state TEXT DEFAULT NULL,
    owner TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_breaker_state ON learning_mirror_circuit_breaker(state, created_at);
"""

# M2-specific idempotent migration statements. Must not modify M1 tables.
_M2_MIGRATIONS = [
    # Ensure learning_strategy_observations has required columns (no-op if table created above)
    "ALTER TABLE learning_strategy_observations ADD COLUMN contract_version TEXT",
    "ALTER TABLE learning_strategy_observations ADD COLUMN owner TEXT",
]

_MIGRATIONS = [
    "ALTER TABLE opval_sessions ADD COLUMN root_session_id TEXT",
    "ALTER TABLE opval_sessions ADD COLUMN tool_execution_count INTEGER DEFAULT 0",
    "ALTER TABLE opval_sessions ADD COLUMN session_tool_correctness REAL",
    "ALTER TABLE opval_sessions ADD COLUMN synthetic_reason TEXT",
    "ALTER TABLE opval_turns ADD COLUMN tool_outputs TEXT",
    "ALTER TABLE opval_turns ADD COLUMN tool_execution_score REAL",
]


class OpvalStore:
    """Persistence layer for OPVAL evidence."""

    def __init__(self, conn_or_path: Any):
        if isinstance(conn_or_path, (str, Path)):
            self._conn = sqlite3.connect(str(conn_or_path), check_same_thread=False)
            self._owns_connection = True
            self._conn.row_factory = sqlite3.Row
            self._apply_pragmas()
        else:
            self._conn = conn_or_path
            self._owns_connection = False
            self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _apply_pragmas(self):
        """Enable WAL + busy_timeout to coexist safely with SessionDB."""
        for pragma in _OPVAL_PRAGMAS:
            try:
                self._conn.execute(pragma)
            except sqlite3.OperationalError:
                # WAL may be unsupported on some filesystems; SessionDB will
                # have already fallen back to DELETE mode in that case.
                pass

    def _ensure_schema(self):
        # Older DBs may have opval_* tables created before newer columns were
        # added.  OPVAL_SCHEMA creates indexes that reference those columns
        # (e.g. idx_opval_sessions_root ON opval_sessions(root_session_id)), so
        # the additive column migrations MUST run before the schema script —
        # otherwise executescript aborts on "no such column" and OpvalStore
        # fails to construct, silently disabling OPVAL recording and starving
        # the learning loop.  _run_migrations is idempotent and tolerant of a
        # missing table, so the pre-pass is a no-op on a fresh DB.
        self._run_migrations()
        self._conn.executescript(OPVAL_SCHEMA)
        self._conn.commit()
        self._run_migrations()

    def _run_migrations(self):
        """Apply additive ALTER TABLE migrations if columns are missing.

        Table-aware: each migration's target table is parsed from the statement
        so columns are checked against the correct table.  Tolerant of a missing
        table (fresh DB — schema creation defines every column) and of an
        already-present column (idempotent).
        """
        for migration in _MIGRATIONS:
            try:
                table = migration.split("ALTER TABLE")[1].split("ADD COLUMN")[0].strip()
                col_name = migration.split("ADD COLUMN")[1].split()[0].strip()
                existing_cols = {
                    r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if not existing_cols or col_name in existing_cols:
                    # table absent (fresh DB) or column already present
                    continue
                self._conn.execute(migration)
            except Exception:
                pass
        self._conn.commit()
        self._run_m2_migrations()

    def _run_m2_migrations(self):
        """Idempotent additive migrations for Milestone 2 tables."""
        try:
            cur = self._conn.execute("PRAGMA table_info(learning_strategy_observations)")
            existing_cols = {r["name"] for r in cur.fetchall()}
        except sqlite3.OperationalError:
            # Table does not exist yet; schema creation will define all columns.
            return
        for migration in _M2_MIGRATIONS:
            try:
                col_name = migration.split("ADD COLUMN")[1].split()[0].strip()
                if col_name in existing_cols:
                    continue
                self._conn.execute(migration)
            except Exception:
                pass
        self._conn.commit()

    def insert_session(self, record: Dict[str, Any]) -> None:
        cols = [
            "session_id", "task_id", "parent_session_id", "root_session_id",
            "platform", "primary_domain", "secondary_domains", "start_time",
            "end_time", "turn_count", "tool_execution_count", "outcome", "session_quality_score",
            "session_tool_correctness", "drift_pct", "misalignment_pct", "promotion_score",
            "synthetic", "synthetic_reason", "opval_enabled", "recorded_at",
        ]
        vals = [record.get(c) for c in cols]
        placeholders = ", ".join(["?"] * len(cols))
        self._conn.execute(
            f"INSERT OR REPLACE INTO opval_sessions ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        self._conn.commit()

    def insert_turn(self, record: Dict[str, Any]) -> None:
        cols = [
            "turn_id", "session_id", "turn_number", "outcome", "tool_calls", "tool_outputs",
            "error_class", "latency_ms", "tokens_used", "quality_score", "tool_execution_score",
            "drift_flag", "misalignment_flag", "checkpoint_event", "started_at",
            "ended_at",
        ]
        vals = [record.get(c) for c in cols]
        placeholders = ", ".join(["?"] * len(cols))
        # Turns are append-only evidence; do not silently overwrite.
        self._conn.execute(
            f"INSERT OR IGNORE INTO opval_turns ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        self._conn.commit()

    def insert_readiness_snapshot(self, record: Dict[str, Any]) -> None:
        cols = ["snapshot_id", "computed_at", "gate_id", "value", "pass", "window_start", "window_end"]
        vals = [record.get(c) for c in cols]
        placeholders = ", ".join(["?"] * len(cols))
        self._conn.execute(
            f"INSERT OR REPLACE INTO opval_readiness_snapshots ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        self._conn.commit()

    def insert_report(self, record: Dict[str, Any]) -> None:
        cols = ["report_id", "report_type", "generated_at", "period_start", "period_end", "payload_json", "delivered_to"]
        vals = [record.get(c) for c in cols]
        placeholders = ", ".join(["?"] * len(cols))
        self._conn.execute(
            f"INSERT OR REPLACE INTO opval_reports ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        self._conn.commit()

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute("SELECT * FROM opval_sessions WHERE session_id=?", (session_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_turns(self, session_id: str) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM opval_turns WHERE session_id=? ORDER BY turn_number",
            (session_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    def validate_in_state_db(
        self,
        session_ids: List[str],
        state_db_conn: sqlite3.Connection,
        since: float,
    ) -> Set[str]:
        """Return session_ids that exist in state.db sessions table within the time window."""
        valid: Set[str] = set()
        if not session_ids:
            return valid
        use_index = state_db_conn.row_factory is sqlite3.Row
        # state.db sessions table has session_id TEXT PRIMARY KEY and created_at / updated_at columns
        # Try created_at first, then updated_at fallback.
        try:
            cur = state_db_conn.execute(
                "SELECT session_id FROM sessions WHERE session_id IN ({}) AND (created_at >= ? OR updated_at >= ?)".format(
                    ",".join("?" * len(session_ids))
                ),
                session_ids + [since, since],
            )
            rows = cur.fetchall()
            valid = {r["session_id"] if use_index else r[0] for r in rows}
        except sqlite3.OperationalError:
            # sessions table may not have created_at/updated_at; just check existence
            cur = state_db_conn.execute(
                "SELECT session_id FROM sessions WHERE session_id IN ({})".format(
                    ",".join("?" * len(session_ids))
                ),
                session_ids,
            )
            rows = cur.fetchall()
            valid = {r["session_id"] if use_index else r[0] for r in rows}
        return valid

    def get_eligible_sessions(
        self,
        since: float,
        min_turns: int = 3,
        min_tool_executions: int = 1,
        state_db_conn: Optional[sqlite3.Connection] = None,
    ) -> List[Dict[str, Any]]:
        """Return RG01-eligible sessions within window with required evidence."""
        cur = self._conn.execute(
            """
            SELECT * FROM opval_sessions
            WHERE synthetic = 0
              AND outcome != 'incomplete'
              AND start_time >= ?
              AND turn_count >= ?
              AND tool_execution_count >= ?
              AND opval_enabled = 1
            """,
            (since, min_turns, min_tool_executions),
        )
        sessions = [dict(r) for r in cur.fetchall()]
        if state_db_conn is not None:
            sids = [s["session_id"] for s in sessions]
            valid = self.validate_in_state_db(sids, state_db_conn, since)
            sessions = [s for s in sessions if s["session_id"] in valid]
        return sessions

    def count_sessions(
        self,
        synthetic: Optional[int] = None,
        domain: Optional[str] = None,
        since: Optional[float] = None,
        min_turns: Optional[int] = None,
        min_tool_executions: Optional[int] = None,
        state_db_conn: Optional[sqlite3.Connection] = None,
    ) -> int:
        """Count distinct root operational sessions, not runtime sessions."""
        eligible = self.get_eligible_sessions(
            since=since or 0,
            min_turns=min_turns or 0,
            min_tool_executions=min_tool_executions or 0,
            state_db_conn=state_db_conn,
        )
        if synthetic is not None:
            eligible = [s for s in eligible if s.get("synthetic") == synthetic]
        if domain is not None:
            eligible = [s for s in eligible if s.get("primary_domain") == domain]
        return len({s.get("root_session_id") or s["session_id"] for s in eligible})

    def list_domain_counts(
        self,
        min_sessions: int = 0,
        since: Optional[float] = None,
        min_turns: Optional[int] = None,
        min_tool_executions: Optional[int] = None,
        state_db_conn: Optional[sqlite3.Connection] = None,
        primary_only: bool = True,
    ) -> Dict[str, int]:
        """Count distinct root sessions per primary domain. Secondary ignored for gate coverage."""
        eligible = self.get_eligible_sessions(
            since=since or 0,
            min_turns=min_turns or 0,
            min_tool_executions=min_tool_executions or 0,
            state_db_conn=state_db_conn,
        )
        counts: Dict[str, Set[str]] = {}
        for s in eligible:
            d = s.get("primary_domain")
            if not d:
                continue
            root = s.get("root_session_id") or s["session_id"]
            counts.setdefault(d, set()).add(root)
        result = {d: len(roots) for d, roots in counts.items()}
        if min_sessions:
            result = {d: c for d, c in result.items() if c >= min_sessions}
        return result

    def get_rolling_turn_stats(
        self,
        window_start: float,
        window_end: float,
        eligible_session_ids: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """Rolling turn stats restricted to eligible sessions."""
        params: List[Any] = [window_start, window_end, window_start]
        sid_filter = "1=1"
        if eligible_session_ids:
            sid_filter = "t.session_id IN ({})".format(",".join("?" * len(eligible_session_ids)))
            params.extend(sorted(eligible_session_ids))
        cur = self._conn.execute(
            f"""
            SELECT
                COUNT(*) as total_turns,
                SUM(CASE WHEN t.outcome='success' THEN 1 ELSE 0 END) as success_turns,
                SUM(CASE WHEN t.outcome IN ('failure','error') THEN 1 ELSE 0 END) as error_turns,
                SUM(t.drift_flag) as drifted_turns,
                SUM(t.misalignment_flag) as misaligned_turns,
                SUM(CASE WHEN t.drift_flag=1 THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(*), 0) as drift_pct,
                SUM(CASE WHEN t.misalignment_flag=1 THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(*), 0) as misalignment_pct
            FROM opval_turns t
            JOIN opval_sessions s ON s.session_id = t.session_id
            WHERE t.ended_at >= ? AND t.ended_at < ?
              AND s.synthetic = 0
              AND s.outcome != 'incomplete'
              AND s.start_time >= ?
              AND {sid_filter}
            """,
            params,
        )
        return dict(cur.fetchone())

    def get_recent_sessions(
        self,
        window_start: float,
        window_end: float,
        eligible_session_ids: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        sid_filter = "1=1"
        params: List[Any] = [window_start, window_end]
        if eligible_session_ids:
            sid_filter = "session_id IN ({})".format(",".join("?" * len(eligible_session_ids)))
            params.extend(sorted(eligible_session_ids))
        cur = self._conn.execute(
            f"""
            SELECT * FROM opval_sessions
            WHERE start_time >= ? AND start_time < ?
              AND synthetic=0 AND outcome != 'incomplete'
              AND {sid_filter}
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]

    def get_tool_scores_for_sessions(
        self,
        session_ids: Iterable[str],
    ) -> Tuple[float, int]:
        """Return (average_tool_execution_score, total_tool_turns) across eligible turns."""
        sids = list(session_ids)
        if not sids:
            return 0.0, 0
        cur = self._conn.execute(
            f"""
            SELECT AVG(tool_execution_score) as avg_score, COUNT(*) as n
            FROM opval_turns
            WHERE session_id IN ({','.join('?' * len(sids))})
              AND tool_outputs IS NOT NULL
              AND tool_outputs != '[]'
            """,
            sids,
        )
        row = cur.fetchone()
        return float(row["avg_score"] or 0.0), int(row["n"] or 0)

    def resolve_root_session_id(
        self,
        session_id: str,
        state_db_conn: Optional[sqlite3.Connection] = None,
    ) -> str:
        """Walk parent_session_id chain to find root; fall back to state.db lineage."""
        seen: Set[str] = set()
        current = session_id
        while current:
            if current in seen:
                break
            seen.add(current)
            row = self._conn.execute(
                "SELECT parent_session_id FROM opval_sessions WHERE session_id=?",
                (current,),
            ).fetchone()
            parent = row["parent_session_id"] if row else None
            if not parent:
                break
            current = parent
        if current == session_id and state_db_conn is not None:
            # Try state.db lineage fallback
            try:
                cur = state_db_conn.execute(
                    "SELECT parent_session_id FROM sessions WHERE session_id=?",
                    (session_id,),
                )
                row = cur.fetchone()
                if row and row["parent_session_id"]:
                    return row["parent_session_id"]
            except Exception:
                pass
        return current

    def close(self) -> None:
        """Explicitly close the underlying SQLite connection only if we own it.

        When OpvalStore is instantiated with a shared connection (e.g., from
        SessionDB), this method is a no-op so the lifecycle owner remains in
        control. Only connections opened by OpvalStore itself are closed.
        """
        if self._owns_connection and self._conn is not None:
            try:
                self._conn.commit()
            finally:
                self._conn.close()
                self._conn = None  # type: ignore[assignment]
