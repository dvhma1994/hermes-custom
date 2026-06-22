"""
Hermes dashboard API routers.

This package is the extraction target for the monolithic ``web_server.py``.
Each submodule is a FastAPI ``APIRouter`` scoped to a single domain.
"""

from fastapi import APIRouter

from hermes_cli.dashboard_api import status, opval, themes


def build_dashboard_router() -> APIRouter:
    """Return the dashboard skin/theme API router."""
    router = APIRouter(tags=["dashboard"])
    router.include_router(themes.router, prefix="/dashboard")
    return router
