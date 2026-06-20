"""OPVAL reporting modules."""

import json
import time
import uuid
from typing import Any, Dict, List, Optional

from .store import OpvalStore
from .readiness import ReadinessGateEngine


class EvidenceHealthReport:
    """Data quality and ingestion health report."""

    def __init__(self, store: OpvalStore):
        self._store = store

    def generate(self, window_days: int = 30) -> Dict[str, Any]:
        now = time.time()
        window_start = now - window_days * 24 * 3600
        total_sessions = self._store.count_sessions()
        synthetic_sessions = self._store.count_sessions(synthetic=1)
        real_sessions = self._store.count_sessions(synthetic=0)
        recent_sessions = self._store.get_recent_sessions(window_start, now)
        recent_real = [s for s in recent_sessions if not s.get("synthetic")]
        recent_synthetic = [s for s in recent_sessions if s.get("synthetic")]

        return {
            "report_type": "evidence_health",
            "generated_at": now,
            "period_start": window_start,
            "period_end": now,
            "total_sessions": total_sessions,
            "real_sessions": real_sessions,
            "synthetic_sessions": synthetic_sessions,
            "recent_real_sessions": len(recent_real),
            "recent_synthetic_sessions": len(recent_synthetic),
            "synthetic_ratio": (
                synthetic_sessions / max(total_sessions, 1)
            ),
            "flags": {
                "high_synthetic_ratio": synthetic_sessions / max(total_sessions, 1) > 0.20,
                "no_recent_real_sessions": len(recent_real) == 0,
                "missing_domain_coverage": len(self._store.list_domain_counts(min_sessions=1)) == 0,
            },
        }


class MonthlyReportGenerator:
    """Generate monthly validation report."""

    def __init__(self, store: OpvalStore):
        self._store = store
        self._readiness = ReadinessGateEngine(store)

    def generate(self, period_start: Optional[float] = None, period_end: Optional[float] = None) -> Dict[str, Any]:
        now = time.time()
        import calendar
        import datetime as _datetime
        if period_start is None or period_end is None:
            from datetime import datetime as _dt
            dt = _dt.utcfromtimestamp(now)
            year, month = dt.year, dt.month
            _, end_day = calendar.monthrange(year, month)
            period_start = _datetime.datetime(year, month, 1, 0, 0, 0).timestamp()
            period_end = _datetime.datetime(year, month, end_day, 23, 59, 59).timestamp()

        readiness = self._readiness.compute(window_days=30)
        health = EvidenceHealthReport(self._store).generate(window_days=30)

        sessions = self._store.get_recent_sessions(period_start, period_end)
        domain_counts = self._store.list_domain_counts(min_sessions=0, since=period_start)
        top_errors: Dict[str, int] = {}
        for s in sessions:
            turns = self._store.get_turns(s["session_id"])
            for t in turns:
                ec = t.get("error_class")
                if ec:
                    top_errors[ec] = top_errors.get(ec, 0) + 1

        report = {
            "report_id": f"monthly-{int(period_start)}-{uuid.uuid4().hex[:8]}",
            "report_type": "monthly_validation",
            "generated_at": now,
            "period_start": period_start,
            "period_end": period_end,
            "executive_summary": {
                "all_gates_pass": readiness["all_pass"],
                "recommendation": self._recommendation(readiness, health),
            },
            "readiness_gates": readiness["gates"],
            "domain_coverage": domain_counts,
            "session_volume": len(sessions),
            "top_error_classes": dict(sorted(top_errors.items(), key=lambda kv: kv[1], reverse=True)[:5]),
            "evidence_health": health,
        }

        self._store.insert_report({
            "report_id": report["report_id"],
            "report_type": "monthly_validation",
            "generated_at": now,
            "period_start": period_start,
            "period_end": period_end,
            "payload_json": json.dumps(report),
            "delivered_to": "",
        })
        return report

    def _recommendation(self, readiness: Dict[str, Any], health: Dict[str, Any]) -> str:
        if health["flags"]["no_recent_real_sessions"]:
            return "Extend OPVAL collection; no real sessions recorded recently."
        if health["flags"]["high_synthetic_ratio"]:
            return "Investigate synthetic session ratio; gates may be inflated."
        if readiness["all_pass"]:
            return "All readiness gates pass. Submit to Learning Governance for unfreeze approval."
        failed = [g["name"] for g in readiness["gates"] if not g["pass"]]
        return f"Gates not met: {', '.join(failed)}. Continue evidence collection and remediation."
