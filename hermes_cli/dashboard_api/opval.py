"""Dashboard API endpoints for OPVAL reporting.

Does NOT activate Strategic Learning or modify Architecture Freeze v1.
Only serves collected evidence and readiness gate status.
"""

import os
import json
import time
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from agent.opval.store import OpvalStore
from agent.opval.readiness import ReadinessGateEngine
from agent.opval.reports import EvidenceHealthReport, MonthlyReportGenerator


router = APIRouter(prefix="/api/opval", tags=["opval"])

HERMES_STATE_PATH = os.path.expanduser("~/AppData/Local/hermes/state.db")


def _get_store() -> OpvalStore:
    # Use the global Hermes state database for evidence storage.
    return OpvalStore(HERMES_STATE_PATH)


@router.get("/status")
async def opval_status() -> Dict[str, Any]:
    """Return OPVAL activation status and basic ingestion stats."""
    enabled = os.getenv("HERMES_OPVAL") == "1"
    store = _get_store()
    total = store.count_sessions()
    real = store.count_sessions(synthetic=0)
    synthetic = store.count_sessions(synthetic=1)
    return {
        "opval_enabled": enabled,
        "total_sessions": total,
        "real_sessions": real,
        "synthetic_sessions": synthetic,
        "state_db": HERMES_STATE_PATH,
    }


@router.get("/readiness")
async def opval_readiness() -> Dict[str, Any]:
    """Return current readiness gate values and pass/fail status."""
    store = _get_store()
    engine = ReadinessGateEngine(store)
    return engine.compute(window_days=30)


@router.get("/health")
async def opval_health() -> Dict[str, Any]:
    """Return evidence health report."""
    store = _get_store()
    return EvidenceHealthReport(store).generate(window_days=30)


@router.get("/reports")
async def opval_reports(limit: int = 10) -> Dict[str, Any]:
    """List generated validation reports."""
    store = _get_store()
    cur = store._conn.execute(
        "SELECT report_id, report_type, generated_at FROM opval_reports ORDER BY generated_at DESC LIMIT ?",
        (limit,),
    )
    rows = [{"report_id": r["report_id"], "report_type": r["report_type"], "generated_at": r["generated_at"]} for r in cur.fetchall()]
    return {"reports": rows}


@router.post("/reports/monthly")
async def opval_generate_monthly(year: int = None, month: int = None) -> Dict[str, Any]:
    """Generate a monthly validation report on demand."""
    store = _get_store()
    gen = MonthlyReportGenerator(store)
    report = gen.generate(year=year, month=month)
    return {"report_id": report["report_id"], "recommendation": report["executive_summary"]["recommendation"]}
