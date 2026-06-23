"""Learning dataset manager for Learning Governance V1.

Curates training and evaluation dataset batches from learning evidence tables.
Each batch is versioned and tracks source hash lineage. The manager reads from
learning_strategy_observations and learning_cases (Milestone 2) and writes only
to learning_dataset_batches.

This module is the sole writer of learning_dataset_batches.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent import learning_constants as lc


@dataclass(frozen=True)
class DatasetBatch:
    batch_id: str
    strategy_id: str
    source_hash: str
    observation_count: int
    case_count: int
    batch_status: str
    owner: str
    created_at: float


class LearningDatasetManager:
    """Dataset manager. Sole writer of learning_dataset_batches."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def build_batch(self, strategy_id: str, now: Optional[float] = None) -> DatasetBatch:
        """Build a new dataset batch for a strategy from current evidence."""
        import time

        now = now or time.time()

        observations = self._fetch_observations(strategy_id)
        cases = self._fetch_cases(strategy_id)

        # Exclude synthetic observations from training set metadata.
        # 'synthetic' column lives in opval_sessions, so fetch from payload.
        real_obs = []
        for o in observations:
            payload = json.loads(o.get("payload_json", "{}"))
            if payload.get("synthetic") == 0:
                real_obs.append(o)
        source_hash = self._compute_source_hash(real_obs, cases)

        batch = DatasetBatch(
            batch_id=str(uuid.uuid4()),
            strategy_id=strategy_id,
            source_hash=source_hash,
            observation_count=len(real_obs),
            case_count=len(cases),
            batch_status=lc.DATASET_BATCH_STATUS_BUILDING,
            owner=lc.OWNER_LEARNING_GOVERNANCE,
            created_at=now,
        )

        self._conn.execute(
            """
            INSERT INTO learning_dataset_batches (
                batch_id, strategy_id, source_hash, observation_count, case_count,
                batch_status, owner, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch.batch_id,
                batch.strategy_id,
                batch.source_hash,
                batch.observation_count,
                batch.case_count,
                batch.batch_status,
                batch.owner,
                batch.created_at,
            ),
        )
        self._conn.commit()
        return batch

    def finalize_batch(self, batch_id: str) -> Optional[DatasetBatch]:
        self._conn.execute(
            "UPDATE learning_dataset_batches SET batch_status=? WHERE batch_id=?",
            (lc.DATASET_BATCH_STATUS_READY, batch_id),
        )
        self._conn.commit()
        return self._get_batch(batch_id)

    def deprecate_batch(self, batch_id: str) -> Optional[DatasetBatch]:
        self._conn.execute(
            "UPDATE learning_dataset_batches SET batch_status=? WHERE batch_id=?",
            (lc.DATASET_BATCH_STATUS_DEPRECATED, batch_id),
        )
        self._conn.commit()
        return self._get_batch(batch_id)

    def get_latest_ready_batch(self, strategy_id: str) -> Optional[DatasetBatch]:
        cur = self._conn.execute(
            """
            SELECT * FROM learning_dataset_batches
            WHERE strategy_id=? AND batch_status=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (strategy_id, lc.DATASET_BATCH_STATUS_READY),
        )
        row = cur.fetchone()
        return self._row_to_batch(dict(row)) if row else None

    def list_batches(self, strategy_id: str) -> List[DatasetBatch]:
        cur = self._conn.execute(
            "SELECT * FROM learning_dataset_batches WHERE strategy_id=? ORDER BY created_at DESC",
            (strategy_id,),
        )
        return [self._row_to_batch(dict(r)) for r in cur.fetchall()]

    def _fetch_observations(self, strategy_id: str) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT * FROM learning_strategy_observations
            WHERE strategy_id=?
            """,
            (strategy_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    def _fetch_cases(self, strategy_id: str) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            """
            SELECT c.* FROM learning_cases c
            JOIN learning_strategy_observations o ON c.observation_id = o.observation_id
            WHERE o.strategy_id=?
            """,
            (strategy_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    def _compute_source_hash(self, observations: List[Dict[str, Any]], cases: List[Dict[str, Any]]) -> str:
        payload = {
            "observation_ids": sorted([o["observation_id"] for o in observations]),
            "case_ids": sorted([c["case_id"] for c in cases]),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def _get_batch(self, batch_id: str) -> Optional[DatasetBatch]:
        cur = self._conn.execute("SELECT * FROM learning_dataset_batches WHERE batch_id=?", (batch_id,))
        row = cur.fetchone()
        return self._row_to_batch(dict(row)) if row else None

    def _row_to_batch(self, row: Dict[str, Any]) -> DatasetBatch:
        return DatasetBatch(
            batch_id=row["batch_id"],
            strategy_id=row["strategy_id"],
            source_hash=row["source_hash"],
            observation_count=row["observation_count"],
            case_count=row["case_count"],
            batch_status=row["batch_status"],
            owner=row["owner"],
            created_at=row["created_at"],
        )
