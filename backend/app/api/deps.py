"""Shared API dependencies.

Central place for FastAPI dependency-injection wiring. Endpoints import these
typed aliases instead of importing infrastructure modules directly, keeping
the composition root explicit and the handlers thin. This is where database
sessions, cache clients, and settings are injected into the request scope.
"""

from __future__ import annotations

from typing import Annotated

import redis.asyncio as redis
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_redis
from app.core.config import Settings, get_settings
from app.database import get_session

# Typed dependency aliases — use these in endpoint signatures.
SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[redis.Redis, Depends(get_redis)]
