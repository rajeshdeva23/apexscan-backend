"""Non-sensitive liveness, readiness, and startup health endpoints."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, Response
from starlette.responses import JSONResponse

from app.core.lifecycle import ApplicationLifecycle

router = APIRouter(tags=["health"])


def _lifecycle(request: Request) -> ApplicationLifecycle:
    """Return the lifecycle owner injected by the application factory."""
    return cast(ApplicationLifecycle, request.app.state.lifecycle)


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Return process liveness without probing transient dependencies.

    Returns:
        A mapping with ``status: "live"`` while the process can serve probes.
    """
    return {"status": "live"}


@router.get("/health/ready", summary="Readiness probe")
async def readiness(request: Request) -> Response:
    """Return dependency-backed readiness without exposing failure details."""
    snapshot = await _lifecycle(request).readiness_snapshot()
    status_code = 200 if snapshot.status == "ready" else 503
    return JSONResponse(status_code=status_code, content=snapshot.as_dict())


@router.get("/health/startup", summary="Startup probe")
async def startup(request: Request) -> Response:
    """Return whether mandatory initialization has completed successfully."""
    snapshot = _lifecycle(request).startup_snapshot()
    status_code = 200 if snapshot.status == "started" else 503
    return JSONResponse(status_code=status_code, content=snapshot.as_dict())
