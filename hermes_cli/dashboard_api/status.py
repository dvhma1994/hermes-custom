"""
Lightweight dashboard health/readiness endpoints.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from gateway.status import get_running_pid, read_runtime_status

router = APIRouter()


class HealthResponse(BaseModel):
    status: str


class ReadyResponse(BaseModel):
    dashboard: bool
    gateway: bool
    version: str


try:
    from hermes_cli import __version__ as _hermes_version
except Exception:  # pragma: no cover
    _hermes_version = "unknown"


@router.get("/health", response_model=HealthResponse)
async def health() -> dict:
    """Always-200 liveness probe."""
    return {"status": "ok"}


@router.get("/ready", response_model=ReadyResponse)
async def ready() -> dict:
    """Readiness probe: dashboard process + gateway liveness."""
    gateway_running = get_running_pid() is not None
    if not gateway_running:
        status = read_runtime_status()
        if status:
            gateway_running = status.get("state") in ("running", "starting")
    return {
        "dashboard": True,
        "gateway": gateway_running,
        "version": _hermes_version,
    }
