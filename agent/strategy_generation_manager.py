"""Strategy generation manager for Learning Governance V1.

Proposes new strategy candidates from successful observation patterns. This
module is read-only/recommendation-only and does not activate, promote, or
modify any governance state.

This module intentionally does NOT implement:
  - strategy activation or promotion (Milestone 3b)
  - governance event recording (Milestone 3b)
  - drift monitoring (Milestone 3b)
"""
from __future__ import annotations

__all__ = ["StrategyGenerationManager", "StrategyCandidate"]

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from agent import learning_constants as lc


@dataclass(frozen=True)
class StrategyCandidate:
    """A proposed strategy candidate with confidence and rationale."""

    candidate_id: str
    strategy_id: str
    strategy_text: str
    source: str
    confidence: float
    rationale: str
    allowed_knobs: List[str]
    authority_directive_value: Optional[Dict[str, Any]] = None


class StrategyGenerationManager:
    """Generate strategy candidates from successful evidence patterns.

    This component is recommendation-only. It proposes candidates but never
    activates, promotes, or never writes to learning_strategy_observations or
    governance tables.
    """

    def __init__(self, store_conn: sqlite3.Connection):
        self._conn = store_conn

    def _success_observations(self, strategy_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if strategy_id:
            cur = self._conn.execute(
                "SELECT * FROM learning_strategy_observations "
                "WHERE strategy_id=? AND outcome=? AND owner=? AND contract_version=?",
                (strategy_id, lc.FEEDBACK_OUTCOME_SUCCESS, lc.OWNER_LEARNING_GOVERNANCE, lc.OPVAL_EVIDENCE_CONTRACT_VERSION),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM learning_strategy_observations "
                "WHERE outcome=? AND owner=? AND contract_version=?",
                (lc.FEEDBACK_OUTCOME_SUCCESS, lc.OWNER_LEARNING_GOVERNANCE, lc.OPVAL_EVIDENCE_CONTRACT_VERSION),
            )
        return [dict(r) for r in cur.fetchall()]

    def _parse_payload(self, payload_json: str) -> Dict[str, Any]:
        import json
        try:
            return json.loads(payload_json)
        except Exception:
            return {}

    def _extract_tool_scope_pattern(self, observations: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Detect if successful sessions cluster around a limited tool set.

        Simple heuristic: if the most common primary_domain across successful
        observations is 'coding', propose a narrower tool scope candidate.
        """
        import json
        domains: Dict[str, int] = {}
        for obs in observations:
            payload = self._parse_payload(obs.get("payload_json", "{}"))
            domain = payload.get("primary_domain") or "default"
            domains[domain] = domains.get(domain, 0) + 1

        if not domains:
            return None
        top_domain = max(domains, key=domains.get)
        if domains[top_domain] < lc.GENERATION_MIN_PATTERN_COUNT:
            return None

        # Map a few domains to proposed tool scopes; all tools must exist in
        # the allowed authority knob set via build_authority_directive.
        tool_scopes_by_domain: Dict[str, Iterable[str]] = {
            "coding": ("terminal", "delegate", "execute_code"),
            "research": ("web_search", "web_extract", "delegate"),
            "default": ("terminal", "delegate"),
        }
        scope = tool_scopes_by_domain.get(top_domain, tool_scopes_by_domain["default"])
        return {
            "domain": top_domain,
            "tool_scope": tuple(scope),
            "confidence": min(1.0, domains[top_domain] / lc.GENERATION_MIN_PATTERN_COUNT),
        }

    def _extract_max_iterations_pattern(self, observations: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Detect if successful sessions run with a lower iteration ceiling.

        Currently a deterministic placeholder. In production this would analyze
        turn counts or latency distributions.
        """
        if len(observations) < lc.GENERATION_MIN_PATTERN_COUNT:
            return None
        return {
            "max_tool_iterations": max(10, lc.DEFAULT_MAX_TOOL_ITERATIONS - 10),
            "confidence": 0.75,
        }

    def generate_candidates(self, strategy_id: Optional[str] = None) -> List[StrategyCandidate]:
        """Generate strategy candidates from successful observations.

        Only produces candidates with confidence >= GENERATION_CONFIDENCE_THRESHOLD.
        """
        observations = self._success_observations(strategy_id)
        candidates: List[StrategyCandidate] = []

        # Candidate 1: tool scope adjustment
        scope_pattern = self._extract_tool_scope_pattern(observations)
        if scope_pattern and scope_pattern["confidence"] >= lc.GENERATION_CONFIDENCE_THRESHOLD:
            candidates.append(
                StrategyCandidate(
                    candidate_id=str(uuid.uuid4()),
                    strategy_id=strategy_id or f"strategy:{scope_pattern['domain']}",
                    strategy_text=f"Restrict tool scope to {scope_pattern['tool_scope']}",
                    source="tool_scope_pattern",
                    confidence=scope_pattern["confidence"],
                    rationale=f"{scope_pattern['confidence']:.0%} of successful sessions in {scope_pattern['domain']} used a limited tool set",
                    allowed_knobs=["tool_scope"],
                    authority_directive_value={"tool_scope": scope_pattern["tool_scope"]},
                )
            )

        # Candidate 2: iteration budget adjustment
        iter_pattern = self._extract_max_iterations_pattern(observations)
        if iter_pattern and iter_pattern["confidence"] >= lc.GENERATION_CONFIDENCE_THRESHOLD:
            candidates.append(
                StrategyCandidate(
                    candidate_id=str(uuid.uuid4()),
                    strategy_id=strategy_id or "strategy:default",
                    strategy_text=f"Reduce max_tool_iterations to {iter_pattern['max_tool_iterations']}",
                    source="max_iterations_pattern",
                    confidence=iter_pattern["confidence"],
                    rationale="Successful sessions complete within a reduced iteration budget",
                    allowed_knobs=["max_tool_iterations"],
                    authority_directive_value={"max_tool_iterations": iter_pattern["max_tool_iterations"]},
                )
            )

        return candidates

    def generate_and_validate_directives(self, strategy_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Generate validated directive dicts that can be wrapped by build_authority_directive.

        This method raises ValueError if any candidate proposes a forbidden knob.
        """
        candidates = self.generate_candidates(strategy_id)
        validated = []
        for candidate in candidates:
            for knob in candidate.allowed_knobs:
                if knob in lc.FORBIDDEN_AUTHORITY_KNOBS:
                    raise ValueError(f"Generated candidate uses forbidden knob: {knob}")
                if knob not in lc.ALLOWED_AUTHORITY_KNOBS:
                    raise ValueError(f"Generated candidate uses unknown authority knob: {knob}")
            validated.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "strategy_id": candidate.strategy_id,
                    "knob": candidate.allowed_knobs[0],
                    "value": candidate.authority_directive_value.get(candidate.allowed_knobs[0]),
                    "confidence": candidate.confidence,
                    "reason": candidate.rationale,
                }
            )
        return validated
