"""Version endpoint.

Exposes build/runtime metadata (app name, version, environment) so clients
and deployment tooling can confirm exactly what is running.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import SettingsDep

router = APIRouter(tags=["meta"])


@router.get("/version", summary="Application version and environment")
async def version(settings: SettingsDep) -> dict[str, str]:
    """Return application identity and version metadata.

    Args:
        settings: Injected application settings.

    Returns:
        Mapping of application name, semantic version, and environment.
    """
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
    }
