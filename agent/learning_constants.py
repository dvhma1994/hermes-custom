"""Immutable constants for Learning Governance V1."""
from __future__ import annotations

__all__ = [
    # OPVAL evidence contract
    "OPVAL_EVIDENCE_CONTRACT_VERSION",
    # Promotion / retirement thresholds (V3 design alignment)
    "LEARNING_PROMOTION_THRESHOLD_SCORE",
    "LEARNING_PROMOTION_MAX_DRIFT_PERCENT",
    "LEARNING_PROMOTION_MAX_MISALIGNMENT_PERCENT",
    "LEARNING_PROMOTION_MIN_REAL_SESSIONS",
    "LEARNING_PROMOTION_MIN_DOMAIN_COVERAGE",
    "LEARNING_RETIREMENT_THRESHOLD_SCORE",
    # V3 design promotion/retirement constants
    "PROMOTION_WIN_RATE",
    "PROMOTION_SAMPLE_COUNT",
    "PROMOTION_AVG_SCORE",
    "PROMOTION_AVG_ALIGNMENT",
    "RETIREMENT_WIN_RATE",
    "RETIREMENT_SAMPLE_COUNT",
    # Readiness gate thresholds
    "READINESS_MIN_SESSIONS",
    "READINESS_MIN_DOMAIN_COVERAGE",
    "READINESS_MAX_DRIFT_PERCENT",
    "READINESS_MAX_MISALIGNMENT_PERCENT",
    "READINESS_MIN_SCORE",
    # Authority knobs
    "ALLOWED_AUTHORITY_KNOBS",
    "FORBIDDEN_AUTHORITY_KNOBS",
    "DEFAULT_MAX_TOOL_ITERATIONS",
    # Authority policies
    "AUTHORITY_POLICY_STRICT",
    "AUTHORITY_POLICY_PERMISSIVE",
    "AUTHORITY_POLICY_NO_SELF_DELEGATE",
    # Feedback outcomes
    "FEEDBACK_OUTCOME_SUCCESS",
    "FEEDBACK_OUTCOME_FAILURE",
    "FEEDBACK_OUTCOME_DEFERRED",
    "MAX_FEEDBACK_EVENTS_PER_TURN",
    # Evidence ownership
    "OWNER_LEARNING_GOVERNANCE",
    # M3a: effectiveness thresholds and recommendation queue
    "DEMOTION_RECOMMENDATION_MAX_AGE_DAYS",
    "DEMOTION_QUEUE_POLL_INTERVAL_HOURS",
    "EFFECTIVENESS_MIN_SAMPLE_COUNT",
    "EFFECTIVENESS_WIN_RATE_THRESHOLD",
    "EFFECTIVENESS_AVG_SCORE_THRESHOLD",
    "EFFECTIVENESS_AVG_ALIGNMENT_THRESHOLD",
    "GENERATION_MIN_PATTERN_COUNT",
    "GENERATION_CONFIDENCE_THRESHOLD",
    # M3b: governance, drift, dataset, circuit breaker
    "GOVERNANCE_EVENT_PROMOTE",
    "GOVERNANCE_EVENT_RETIRE",
    "GOVERNANCE_EVENT_ENFORCE",
    "GOVERNANCE_EVENT_CIRCUIT_BREAKER_ACTIVATE",
    "GOVERNANCE_EVENT_RESTORE",
    "GOVERNANCE_STATUS_PENDING",
    "GOVERNANCE_STATUS_APPLIED",
    "GOVERNANCE_STATUS_REJECTED",
    "DRIFT_MONITOR_INTERVAL_HOURS",
    "DRIFT_MAX_SNAPSHOT_AGE_DAYS",
    "DATASET_BATCH_STATUS_BUILDING",
    "DATASET_BATCH_STATUS_READY",
    "DATASET_BATCH_STATUS_DEPRECATED",
    "CIRCUIT_BREAKER_DEGRADED_TIMEOUT_HOURS",
    "CIRCUIT_BREAKER_FORCED_REFREEZE_HOURS",
    "CIRCUIT_BREAKER_STATE_HEALTHY",
    "CIRCUIT_BREAKER_STATE_DEGRADED",
    "CIRCUIT_BREAKER_STATE_REFROZEN",
    # Promotion constants list (immutability check)
    "PROMOTION_CONSTANTS",
]

# ──────────────────────────────────────────────────────────────
# OPVAL contract version
# ──────────────────────────────────────────────────────────────
OPVAL_EVIDENCE_CONTRACT_VERSION: str = "1.0.0"

# ──────────────────────────────────────────────────────────────
# Promotion thresholds (immutable; never written by strategy code)
# ──────────────────────────────────────────────────────────────
LEARNING_PROMOTION_THRESHOLD_SCORE: float = 0.80
LEARNING_PROMOTION_MAX_DRIFT_PERCENT: float = 0.15
LEARNING_PROMOTION_MAX_MISALIGNMENT_PERCENT: float = 0.10
LEARNING_PROMOTION_MIN_REAL_SESSIONS: int = 50
LEARNING_PROMOTION_MIN_DOMAIN_COVERAGE: int = 5

LEARNING_RETIREMENT_THRESHOLD_SCORE: float = 0.55

# ──────────────────────────────────────────────────────────────
# V3-design promotion/retirement constants
# ──────────────────────────────────────────────────────────────
PROMOTION_WIN_RATE: float = 0.75
PROMOTION_SAMPLE_COUNT: int = 3
PROMOTION_AVG_SCORE: float = 65.0
PROMOTION_AVG_ALIGNMENT: float = 65.0

RETIREMENT_WIN_RATE: float = 0.35
RETIREMENT_SAMPLE_COUNT: int = 3

# ──────────────────────────────────────────────────────────────
# Readiness gate thresholds
# ──────────────────────────────────────────────────────────────
READINESS_MIN_SESSIONS: int = 50
READINESS_MIN_DOMAIN_COVERAGE: int = 5
READINESS_MAX_DRIFT_PERCENT: float = 0.15
READINESS_MAX_MISALIGNMENT_PERCENT: float = 0.10
READINESS_MIN_SCORE: float = 0.80

# ──────────────────────────────────────────────────────────────
# Allowed runtime-authority knobs (Milestone 1)
# ──────────────────────────────────────────────────────────────
ALLOWED_AUTHORITY_KNOBS: frozenset[str] = frozenset({
    "policy",
    "tool_scope",
    "context_size_override",
    "allow_self_delegate",
    "max_tool_iterations",
})

# Knobs that strategy/effectiveness code must never be able to mutate.
FORBIDDEN_AUTHORITY_KNOBS: frozenset[str] = frozenset({
    "promotion_threshold",
    "retirement_threshold",
    "readiness_min_sessions",
    "readiness_min_domain_coverage",
    "opval_contract_version",
})

# Default authority values.
DEFAULT_MAX_TOOL_ITERATIONS: int = 50

# Authority policies.
AUTHORITY_POLICY_STRICT: str = "strict"
AUTHORITY_POLICY_PERMISSIVE: str = "permissive"
AUTHORITY_POLICY_NO_SELF_DELEGATE: str = "no_self_delegate"

# Feedback outcomes.
FEEDBACK_OUTCOME_SUCCESS: str = "success"
FEEDBACK_OUTCOME_FAILURE: str = "failure"
FEEDBACK_OUTCOME_DEFERRED: str = "deferred"

MAX_FEEDBACK_EVENTS_PER_TURN: int = 100

# Evidence ownership label for Learning Governance V1 observations.
OWNER_LEARNING_GOVERNANCE: str = "learning-governance-v1"

# ──────────────────────────────────────────────────────────────
# M3a: effectiveness thresholds and recommendation queue
# ──────────────────────────────────────────────────────────────
DEMOTION_RECOMMENDATION_MAX_AGE_DAYS: int = 7
DEMOTION_QUEUE_POLL_INTERVAL_HOURS: int = 1
EFFECTIVENESS_MIN_SAMPLE_COUNT: int = 3
EFFECTIVENESS_WIN_RATE_THRESHOLD: float = 0.75
EFFECTIVENESS_AVG_SCORE_THRESHOLD: float = 65.0
EFFECTIVENESS_AVG_ALIGNMENT_THRESHOLD: float = 65.0
GENERATION_MIN_PATTERN_COUNT: int = 5
GENERATION_CONFIDENCE_THRESHOLD: float = 0.8

# ──────────────────────────────────────────────────────────────
# M3b: governance, drift, dataset, circuit breaker
# ──────────────────────────────────────────────────────────────
GOVERNANCE_EVENT_PROMOTE: str = "PROMOTE"
GOVERNANCE_EVENT_RETIRE: str = "RETIRE"
GOVERNANCE_EVENT_ENFORCE: str = "ENFORCE"
GOVERNANCE_EVENT_CIRCUIT_BREAKER_ACTIVATE: str = "CIRCUIT_BREAKER_ACTIVATE"
GOVERNANCE_EVENT_RESTORE: str = "RESTORE"

GOVERNANCE_STATUS_PENDING: str = "PENDING"
GOVERNANCE_STATUS_APPLIED: str = "APPLIED"
GOVERNANCE_STATUS_REJECTED: str = "REJECTED"

DRIFT_MONITOR_INTERVAL_HOURS: int = 24
DRIFT_MAX_SNAPSHOT_AGE_DAYS: int = 7

DATASET_BATCH_STATUS_BUILDING: str = "BUILDING"
DATASET_BATCH_STATUS_READY: str = "READY"
DATASET_BATCH_STATUS_DEPRECATED: str = "DEPRECATED"

CIRCUIT_BREAKER_DEGRADED_TIMEOUT_HOURS: int = 24
CIRCUIT_BREAKER_FORCED_REFREEZE_HOURS: int = 24
CIRCUIT_BREAKER_STATE_HEALTHY: str = "HEALTHY"
CIRCUIT_BREAKER_STATE_DEGRADED: str = "DEGRADED"
CIRCUIT_BREAKER_STATE_REFROZEN: str = "REFROZEN"

# List used to verify immutability of promotion constants.
PROMOTION_CONSTANTS: tuple[str, ...] = (
    "OPVAL_EVIDENCE_CONTRACT_VERSION",
    "LEARNING_PROMOTION_THRESHOLD_SCORE",
    "LEARNING_PROMOTION_MAX_DRIFT_PERCENT",
    "LEARNING_PROMOTION_MAX_MISALIGNMENT_PERCENT",
    "LEARNING_PROMOTION_MIN_REAL_SESSIONS",
    "LEARNING_PROMOTION_MIN_DOMAIN_COVERAGE",
    "LEARNING_RETIREMENT_THRESHOLD_SCORE",
    "PROMOTION_WIN_RATE",
    "PROMOTION_SAMPLE_COUNT",
    "PROMOTION_AVG_SCORE",
    "PROMOTION_AVG_ALIGNMENT",
    "RETIREMENT_WIN_RATE",
    "RETIREMENT_SAMPLE_COUNT",
    "READINESS_MIN_SESSIONS",
    "READINESS_MIN_DOMAIN_COVERAGE",
    "READINESS_MAX_DRIFT_PERCENT",
    "READINESS_MAX_MISALIGNMENT_PERCENT",
    "READINESS_MIN_SCORE",
    "DEFAULT_MAX_TOOL_ITERATIONS",
    "MAX_FEEDBACK_EVENTS_PER_TURN",
    "OWNER_LEARNING_GOVERNANCE",
    "DEMOTION_RECOMMENDATION_MAX_AGE_DAYS",
    "DEMOTION_QUEUE_POLL_INTERVAL_HOURS",
    "EFFECTIVENESS_MIN_SAMPLE_COUNT",
    "EFFECTIVENESS_WIN_RATE_THRESHOLD",
    "EFFECTIVENESS_AVG_SCORE_THRESHOLD",
    "EFFECTIVENESS_AVG_ALIGNMENT_THRESHOLD",
    "GENERATION_MIN_PATTERN_COUNT",
    "GENERATION_CONFIDENCE_THRESHOLD",
    "GOVERNANCE_EVENT_PROMOTE",
    "GOVERNANCE_EVENT_RETIRE",
    "GOVERNANCE_EVENT_ENFORCE",
    "GOVERNANCE_EVENT_CIRCUIT_BREAKER_ACTIVATE",
    "GOVERNANCE_EVENT_RESTORE",
    "GOVERNANCE_STATUS_PENDING",
    "GOVERNANCE_STATUS_APPLIED",
    "GOVERNANCE_STATUS_REJECTED",
    "DRIFT_MONITOR_INTERVAL_HOURS",
    "DRIFT_MAX_SNAPSHOT_AGE_DAYS",
    "DATASET_BATCH_STATUS_BUILDING",
    "DATASET_BATCH_STATUS_READY",
    "DATASET_BATCH_STATUS_DEPRECATED",
    "CIRCUIT_BREAKER_DEGRADED_TIMEOUT_HOURS",
    "CIRCUIT_BREAKER_FORCED_REFREEZE_HOURS",
    "CIRCUIT_BREAKER_STATE_HEALTHY",
    "CIRCUIT_BREAKER_STATE_DEGRADED",
    "CIRCUIT_BREAKER_STATE_REFROZEN",
)
