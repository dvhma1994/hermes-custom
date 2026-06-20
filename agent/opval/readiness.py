"""Readiness gate engine."""

import time
import uuid
from typing import Any, Dict, List, Optional, Set

from .store import OpvalStore


class ReadinessGateEngine:
    """Compute readiness gates from OPVAL evidence store."""

    def __init__(self, store: OpvalStore):
        self._store = store

    def compute(
        self,
        window_days: int = 30,
        domain_list: Optional[List[str]] = None,
        now: Optional[float] = None,
        state_db_conn: Optional[Any] = None,
    ) -> Dict[str, Any]:
        now = now or time.time()
        window_start = now - (window_days * 24 * 3600)
        domain_list = domain_list or [
            "runtime_development", "tool_authoring", "skill_library",
            "gateway_operations", "dashboard_ui", "validation_auditing",
            "agentic_coding", "general_assistance",
        ]

        # Build the evidence bar: eligible root sessions
        eligible = self._store.get_eligible_sessions(
            since=window_start,
            min_turns=3,
            min_tool_executions=1,
            state_db_conn=state_db_conn,
        )
        eligible_session_ids: Set[str] = {s["session_id"] for s in eligible}
        eligible_root_ids: Set[str] = {s.get("root_session_id") or s["session_id"] for s in eligible}

        # Minimum evidence requirements
        total_eligible_turns = self._store.get_rolling_turn_stats(
            window_start, now, eligible_session_ids=eligible_session_ids
        ).get("total_turns") or 0
        insufficient_evidence = (
            len(eligible_root_ids) < 10 or total_eligible_turns < 100
        )

        # RG01: count distinct root sessions
        rg01_value = len(eligible_root_ids)
        rg01_pass = (not insufficient_evidence) and rg01_value >= 50

        # RG02: domain coverage from primary_domain only
        primary_domain_counts = self._store.list_domain_counts(
            min_sessions=0,
            since=window_start,
            min_turns=3,
            min_tool_executions=1,
            state_db_conn=state_db_conn,
            primary_only=True,
        )
        domain_coverage_count = sum(
            1 for d in domain_list if primary_domain_counts.get(d, 0) >= 5
        )
        rg02_pass = (not insufficient_evidence) and domain_coverage_count >= len(domain_list)

        # RG03/RG04: from eligible turns only
        stats = self._store.get_rolling_turn_stats(
            window_start, now, eligible_session_ids=eligible_session_ids
        )
        total_turns = stats.get("total_turns") or 0
        if total_turns > 0 and total_eligible_turns >= 100:
            drift_pct = (stats.get("drifted_turns") or 0) / total_turns * 100
            misalignment_pct = (stats.get("misaligned_turns") or 0) / total_turns * 100
        else:
            drift_pct = None
            misalignment_pct = None

        rg03_pass = (drift_pct is not None) and drift_pct <= 15.0
        rg04_pass = (misalignment_pct is not None) and misalignment_pct <= 10.0

        # RG05: promotion score from turn-level evidence
        promotion_score = self._compute_promotion_score(
            eligible, eligible_root_ids, stats, window_start, now, state_db_conn
        )
        rg05_pass = (
            (promotion_score is not None)
            and promotion_score >= 80.0
            and (not insufficient_evidence)
        )

        gates = [
            {
                "gate_id": "RG01",
                "name": "Real Session Volume",
                "value": float(rg01_value),
                "pass": rg01_pass,
                "target": 50,
            },
            {
                "gate_id": "RG02",
                "name": "Domain Coverage",
                "value": float(domain_coverage_count),
                "pass": rg02_pass,
                "target": len(domain_list),
            },
            {
                "gate_id": "RG03",
                "name": "Session Drift",
                "value": float(drift_pct if drift_pct is not None else 0.0),
                "pass": rg03_pass,
                "target": 15.0,
            },
            {
                "gate_id": "RG04",
                "name": "Misalignment Rate",
                "value": float(misalignment_pct if misalignment_pct is not None else 0.0),
                "pass": rg04_pass,
                "target": 10.0,
            },
            {
                "gate_id": "RG05",
                "name": "Promotion Quality Score",
                "value": float(promotion_score if promotion_score is not None else 0.0),
                "pass": rg05_pass,
                "target": 80.0,
            },
        ]

        snapshot_time = time.time()
        for gate in gates:
            self._store.insert_readiness_snapshot({
                "snapshot_id": f"{snapshot_time}-{gate['gate_id']}-{uuid.uuid4().hex[:8]}",
                "computed_at": snapshot_time,
                "gate_id": gate["gate_id"],
                "value": gate["value"],
                "pass": 1 if gate["pass"] else 0,
                "window_start": window_start,
                "window_end": now,
            })

        return {
            "computed_at": snapshot_time,
            "window_start": window_start,
            "window_end": now,
            "all_pass": all(g["pass"] for g in gates),
            "eligible_root_sessions": len(eligible_root_ids),
            "eligible_turns": total_eligible_turns,
            "insufficient_evidence": insufficient_evidence,
            "gates": gates,
        }

    def _compute_promotion_score(
        self,
        eligible_sessions: List[Dict[str, Any]],
        eligible_root_ids: Set[str],
        stats: Dict[str, Any],
        window_start: float,
        window_end: float,
        state_db_conn: Optional[Any] = None,
    ) -> Optional[float]:
        total_turns = stats.get("total_turns") or 0
        success_turns = stats.get("success_turns") or 0
        error_turns = stats.get("error_turns") or 0
        total_sessions = len(eligible_root_ids)
        if total_sessions < 10 or total_turns < 100:
            return None

        completed_root_sessions = set()
        root_error_sessions: Set[str] = set()
        for s in eligible_sessions:
            root = s.get("root_session_id") or s["session_id"]
            if s.get("outcome") == "success":
                completed_root_sessions.add(root)
            turns = self._store.get_turns(s["session_id"])
            if any(t.get("outcome") in ("error", "failure") for t in turns):
                root_error_sessions.add(root)

        completion_rate = len(completed_root_sessions) / max(total_sessions, 1)
        success_rate = success_turns / max(total_turns, 1)

        recovery_rate = self._estimate_recovery(
            eligible_sessions, root_error_sessions
        )

        # Tool correctness from turn-level tool_execution_score
        avg_tool_score, tool_turn_count = self._store.get_tool_scores_for_sessions(
            [s["session_id"] for s in eligible_sessions]
        )
        tool_correctness = avg_tool_score

        user_sat = 1.0 - (stats.get("misaligned_turns") or 0) / max(total_turns, 1)

        targets = {
            "success_rate": 0.90,
            "completion_rate": 0.85,
            "tool_correctness": 0.95,
            "recovery_rate": 0.80,
            "user_sat": 0.80,
        }
        weights = {
            "success_rate": 0.30,
            "completion_rate": 0.25,
            "tool_correctness": 0.20,
            "recovery_rate": 0.15,
            "user_sat": 0.10,
        }
        scores = {
            "success_rate": min(success_rate / targets["success_rate"], 1.0),
            "completion_rate": min(completion_rate / targets["completion_rate"], 1.0),
            "tool_correctness": min(tool_correctness / targets["tool_correctness"], 1.0),
            "recovery_rate": min(recovery_rate / targets["recovery_rate"], 1.0),
            "user_sat": min(user_sat / targets["user_sat"], 1.0),
        }
        score = sum(scores[k] * weights[k] for k in weights) * 100
        return round(score, 2)

    def _estimate_recovery(
        self,
        eligible_sessions: List[Dict[str, Any]],
        root_error_sessions: Set[str],
    ) -> float:
        if not root_error_sessions:
            return 1.0
        recovered = 0
        for s in eligible_sessions:
            root = s.get("root_session_id") or s["session_id"]
            if root in root_error_sessions and s.get("outcome") == "success":
                recovered += 1
        return recovered / max(len(root_error_sessions), 1)
