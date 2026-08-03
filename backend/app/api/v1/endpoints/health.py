"""Health check endpoints.

Liveness/readiness probes for orchestrators (Docker, Kubernetes) and uptime
monitors. Kept dependency-light so a failing database does not mask a live
process — deeper readiness checks can be added as the platform grows.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Return a simple liveness signal.

    Returns:
        A mapping with ``status: "ok"`` when the process is serving requests.
    """
    return {"status": "ok"}
