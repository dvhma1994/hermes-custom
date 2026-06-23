"""Learning evidence builder: sole writer of learning_strategy_observations.

Derives observations from validated OPVAL evidence only, computes a
reproducible source_hash, enforces the OPVAL evidence contract version, and
uses idempotent writes under an advisory lock.

This module intentionally does NOT implement:
  - promotion / retirement decisions (Milestone 3)
  - governance event append-only chain (Milestone 3)
  - strategy generation or drift monitoring (Milestone 3/4)
"""
from __future__ import annotations

__all__ = [
    "OpvalContractError",
    "LearningEvidenceBuilder",
    "compute_source_hash",
    "validate_evidence_ownership",
]

import hashlib
import json
import logging
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

from agent import learning_constants as lc

logger = logging.getLogger(__name__)


class OpvalContractError(Exception):
    """Raised when OPVAL evidence does not match the expected contract version."""


OPVAL_REQUIRED_SESSION_COLUMNS = {
    "session_id", "primary_domain", "outcome", "synthetic", "opval_enabled",
    "start_time", "recorded_at",
}
OPVAL_REQUIRED_TURN_COLUMNS = {
    "turn_id", "session_id", "outcome", "tool_execution_score", "drift_flag",
    "misalignment_flag",
}


def compute_source_hash(payload: Dict[str, Any]) -> str:
    """Return a deterministic SHA-256 hash of an evidence payload.

    Only the contract-relevant fields are serialized in sorted-key order.
    """
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_evidence_ownership(observation: Dict[str, Any]) -> None:
    """Verify an observation carries required ownership metadata."""
    source_module = observation.get("source_module")
    owner = observation.get("owner")
    if not source_module or not owner:
        raise ValueError("Observation missing source_module or owner")
    if owner != lc.OWNER_LEARNING_GOVERNANCE:
        raise ValueError(f"Observation owner must be {lc.OWNER_LEARNING_GOVERNANCE!r}; got {owner!r}")
    # Verify source_hash matches recomputed hash from payload
    payload = observation.get("payload_json")
    if payload:
        recomputed = compute_source_hash(json.loads(payload))
        if recomputed != observation.get("source_hash"):
            raise ValueError("source_hash does not match payload; observation may be tampered")


class LearningEvidenceBuilder:
    """Builds learning observations from OPVAL evidence.

    This class is the SOLE writer of `learning_strategy_observations`. All other
    code paths must call it instead of writing directly to that table.
    """

    def __init__(self, store_conn: sqlite3.Connection, strategy_id: str):
        self._conn = store_conn
        self._strategy_id = strategy_id
        self._contract_version = lc.OPVAL_EVIDENCE_CONTRACT_VERSION

    def _ensure_strategy_exists(self, strategy_id: str, strategy_text: str = "") -> None:
        """Ensure the strategy row exists in learning_strategies."""
        if not strategy_text:
            strategy_text = f"Strategy for {strategy_id}"
        with self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO learning_strategies
                (strategy_id, strategy_text, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (strategy_id, strategy_text, "generated", time.time(), time.time()),
            )

    def _check_contract_version(self) -> None:
        """Validate that persisted evidence matches the active OPVAL contract version.

        Effectiveness queries filter observations by ``contract_version``, so any
        evidence written under a *different* contract version is silently
        invisible to scoring/governance. Detect that drift: if the store holds
        observations whose ``contract_version`` differs from the active one,
        raise :class:`OpvalContractError`. Absence of evidence is not a
        violation. This is the real check the per-derivation
        :meth:`_verify_opval_sample` (structural columns) does not cover.
        """
        try:
            rows = self._conn.execute(
                "SELECT DISTINCT contract_version FROM learning_strategy_observations "
                "WHERE owner=?",
                (lc.OWNER_LEARNING_GOVERNANCE,),
            ).fetchall()
        except sqlite3.OperationalError:
            return  # table not created yet — nothing to validate
        versions = {r[0] for r in rows if r and r[0] is not None}
        incompatible = sorted(v for v in versions if v != self._contract_version)
        if incompatible:
            raise OpvalContractError(
                f"Persisted evidence uses contract version(s) {incompatible} that "
                f"differ from the active {self._contract_version!r}; that evidence "
                "would be silently ignored by effectiveness scoring."
            )

    def _verify_opval_sample(self, session_record: Dict[str, Any], turn_records: List[Dict[str, Any]]) -> None:
        """Raise OpvalContractError if the sample cannot satisfy the contract."""
        missing_session = OPVAL_REQUIRED_SESSION_COLUMNS - set(session_record.keys())
        if missing_session:
            raise OpvalContractError(
                f"OPVAL session missing required columns: {sorted(missing_session)}"
            )
        for turn in turn_records:
            missing_turn = OPVAL_REQUIRED_TURN_COLUMNS - set(turn.keys())
            if missing_turn:
                raise OpvalContractError(
                    f"OPVAL turn {turn.get('turn_id')} missing required columns: {sorted(missing_turn)}"
                )

    def _get_strategy_id(self, record: Dict[str, Any]) -> str:
        """Map an OPVAL session to a strategy id (one strategy per primary_domain).

        Observations are routed by the session's ``primary_domain`` — this is
        intentional and load-bearing: ``StrategyGenerationManager`` and the
        effectiveness/governance lookups all key off ``strategy:{domain}``.

        Contract: a caller that pairs this builder with ``StrategicLearning``
        (whose ``record_case`` validates against ``evidence_builder.strategy_id``)
        MUST construct the builder with the matching domain-derived id. The
        canonical, correct caller is ``learning_cycle`` via
        ``strategy_id_for_domain``; do not hand-construct a builder with an id
        that diverges from the sessions you feed it.
        """
        return f"strategy:{record.get('primary_domain') or 'default'}"

    def derive_observations(
        self,
        opval_session: Dict[str, Any],
        opval_turns: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Derive learning observations from a validated OPVAL session + turns.

        Observations are derived only; they are not persisted until
        `persist_observations` is called.
        """
        self._verify_opval_sample(opval_session, opval_turns)

        observations: List[Dict[str, Any]] = []
        session_id = opval_session["session_id"]
        strategy_id = self._get_strategy_id(opval_session)

        # Session-level observation
        session_payload: Dict[str, Any] = {
            "session_id": session_id,
            "primary_domain": opval_session.get("primary_domain"),
            "outcome": opval_session.get("outcome"),
            "synthetic": opval_session.get("synthetic"),
            "opval_enabled": opval_session.get("opval_enabled"),
            "promotion_score": opval_session.get("promotion_score"),
            # Additive (backward-compatible) signals so effectiveness scoring has
            # real variance to learn from: promotion_score is frequently 0/unset,
            # while these session-level aggregates actually vary across sessions.
            "session_quality_score": opval_session.get("session_quality_score"),
            "session_tool_correctness": opval_session.get("session_tool_correctness"),
            "drift_pct": opval_session.get("drift_pct"),
            "misalignment_pct": opval_session.get("misalignment_pct"),
        }
        observations.append({
            "observation_id": str(uuid.uuid4()),
            "opval_session_id": session_id,
            "strategy_id": strategy_id,
            "evidence_type": "tool",
            "outcome": opval_session.get("outcome", "deferred"),
            "source_hash": compute_source_hash(session_payload),
            "source_module": "agent.opval.store",
            "owner": lc.OWNER_LEARNING_GOVERNANCE,
            "contract_version": self._contract_version,
            "payload_json": json.dumps(session_payload, sort_keys=True, default=str),
            "recorded_at": time.time(),
        })

        # Turn-level observations for failure-bearing turns
        for turn in opval_turns:
            if turn.get("outcome") not in (lc.FEEDBACK_OUTCOME_FAILURE, "error"):
                continue
            payload: Dict[str, Any] = {
                "turn_id": turn["turn_id"],
                "session_id": session_id,
                "outcome": turn.get("outcome"),
                "tool_execution_score": turn.get("tool_execution_score"),
                "drift_flag": turn.get("drift_flag"),
                "misalignment_flag": turn.get("misalignment_flag"),
            }
            observations.append({
                "observation_id": str(uuid.uuid4()),
                "opval_session_id": session_id,
                "strategy_id": strategy_id,
                "evidence_type": "recovery",
                "outcome": turn.get("outcome", lc.FEEDBACK_OUTCOME_FAILURE),
                "source_hash": compute_source_hash(payload),
                "source_module": "agent.opval.store",
                "owner": lc.OWNER_LEARNING_GOVERNANCE,
                "contract_version": self._contract_version,
                "payload_json": json.dumps(payload, sort_keys=True, default=str),
                "recorded_at": time.time(),
            })

        return observations

    def persist_observations(self, observations: List[Dict[str, Any]]) -> int:
        """Atomically persist derived observations using idempotent INSERT OR IGNORE.

        Returns the number of observations actually inserted.
        """
        if not observations:
            return 0

        # Ensure parent strategies exist before inserting observations.
        strategy_ids = {obs["strategy_id"] for obs in observations}
        for strategy_id in strategy_ids:
            self._ensure_strategy_exists(strategy_id)

        inserted = 0
        with self._conn:
            for obs in observations:
                validate_evidence_ownership(obs)
                before = self._conn.total_changes
                try:
                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO learning_strategy_observations
                        (observation_id, opval_session_id, strategy_id, evidence_type,
                         outcome, source_hash, source_module, owner, contract_version,
                         payload_json, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            obs["observation_id"],
                            obs["opval_session_id"],
                            obs["strategy_id"],
                            obs["evidence_type"],
                            obs["outcome"],
                            obs["source_hash"],
                            obs["source_module"],
                            obs["owner"],
                            obs["contract_version"],
                            obs["payload_json"],
                            obs["recorded_at"],
                        ),
                    )
                    inserted += self._conn.total_changes - before
                except sqlite3.IntegrityError as exc:
                    # UNIQUE constraint prevents duplicate observations.
                    if "UNIQUE" not in str(exc):
                        raise
        return inserted

    def build_and_persist(
        self,
        opval_session: Dict[str, Any],
        opval_turns: List[Dict[str, Any]],
    ) -> int:
        """Convenience: derive + persist observations for one OPVAL session.

        Surfaces contract-version drift as a non-fatal warning (so a version
        upgrade does not block new evidence); call :meth:`_check_contract_version`
        directly when you want the hard check.
        """
        try:
            self._check_contract_version()
        except OpvalContractError as exc:
            logger.warning("learning evidence contract drift: %s", exc)
        observations = self.derive_observations(opval_session, opval_turns)
        return self.persist_observations(observations)

    def get_observations(self, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Read back observations (read-only; useful for tests and StrategicLearning)."""
        if session_id:
            cur = self._conn.execute(
                "SELECT * FROM learning_strategy_observations WHERE opval_session_id=?",
                (session_id,),
            )
        else:
            cur = self._conn.execute("SELECT * FROM learning_strategy_observations")
        return [dict(r) for r in cur.fetchall()]

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    @property
    def contract_version(self) -> str:
        return self._contract_version
