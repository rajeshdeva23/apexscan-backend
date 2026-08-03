"""API v1 aggregate router.

Collects every v1 endpoint module into a single ``APIRouter`` that the app
mounts under the versioned prefix. New endpoint modules are wired in here —
this is the one place that knows the full v1 surface.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import health, version

api_router = APIRouter()

# Register endpoint routers. Add future feature routers below.
api_router.include_router(health.router)
api_router.include_router(version.router)
