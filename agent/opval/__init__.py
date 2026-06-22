"""Operational Validation Mode (OPVAL) evidence collection and scoring.

Collects real-session evidence when HERMES_OPVAL=1, runs deterministic
evaluators, and computes readiness gates. Does NOT activate Strategic
Learning or modify Architecture Freeze v1 behavior.
"""

from .store import OpvalStore
from .collectors import SessionCollector, TurnCollector
from .evaluators import IntentExtractor, MisalignmentValidator, OutcomeClassifier
from .readiness import ReadinessGateEngine
from .reports import MonthlyReportGenerator, EvidenceHealthReport
from .integration import OpvalSessionRecorder, opval_enabled

__all__ = [
    "OpvalStore",
    "SessionCollector",
    "TurnCollector",
    "IntentExtractor",
    "MisalignmentValidator",
    "OutcomeClassifier",
    "ReadinessGateEngine",
    "MonthlyReportGenerator",
    "EvidenceHealthReport",
    "OpvalSessionRecorder",
    "opval_enabled",
]
