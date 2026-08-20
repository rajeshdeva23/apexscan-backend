"""Offline validation-harness application factory.

Assembles a FastAPI app that serves the real scanner REST surface over the real
runtime pipeline, but with the external dependencies replaced by offline doubles:
in-memory DB/Redis lifecycles and the synthetic :class:`OfflineFixtureProvider`
(injected through the existing ``LiveMarketRuntimeDependency`` adapter seam). No
PostgreSQL, no Redis, no Dhan, no network.

This composition is dev/validation-only. The production composition root
(``app.main``) is untouched; nothing here is reachable from it. The Dhan settings
are dummy placeholders that are never used, because the injected fixture adapter
fully replaces the broker adapter.
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from app.core.config import Settings, get_settings
from app.core.lifecycle import ApplicationLifecycle
from app.market_engine.clock import ManualClock
from app.services.dhan_runtime_composition import LiveMarketRuntimeDependency, LiveUniverseAdapter
from app.services.offline_harness.fixture_provider import REFERENCE_INSTANT, OfflineFixtureProvider
from app.services.offline_harness.in_memory_lifecycles import (
    InMemoryDatabaseLifecycle,
    InMemoryRedisLifecycle,
)

_OFFLINE_DATABASE_URL = "postgresql+asyncpg://offline:offline@localhost:5432/offline"
_OFFLINE_REDIS_URL = "redis://localhost:6379/0"


def _ensure_offline_env() -> None:
    """Seed the app-level settings env with offline-safe placeholders.

    ``create_app`` reads ``get_settings()`` for title/CORS/prefix, and those settings
    require a DATABASE_URL/REDIS_URL. Seed harmless placeholders (the in-memory
    lifecycles ignore them) and clear the settings cache so the values take effect.
    """
    os.environ.setdefault("DATABASE_URL", _OFFLINE_DATABASE_URL)
    os.environ.setdefault("REDIS_URL", _OFFLINE_REDIS_URL)
    get_settings.cache_clear()


def _offline_provider_settings() -> Settings:
    """Build provider settings with the market runtime enabled and dummy Dhan auth.

    ``market_provider_enabled=True`` selects the enabled composition path; the injected
    fixture adapter replaces the Dhan adapter, so the placeholder access token is never
    read or transmitted.
    """
    return Settings(
        app_env="development",
        database_url=_OFFLINE_DATABASE_URL,
        redis_url=_OFFLINE_REDIS_URL,
        market_provider_enabled=True,
        dhan_auth_mode="access_token",
        dhan_access_token="offline-placeholder-unused",
        strategies_enabled=os.environ.get(
            "OFFLINE_STRATEGIES_ENABLED", "narrow_cpr,previous_session_range_pct"
        ),
    )


def create_offline_app() -> FastAPI:
    """Build the offline harness FastAPI app (real pipeline, offline dependencies)."""
    _ensure_offline_env()
    # Imported after env seeding: importing app.main builds a module-level production
    # app via get_settings(), which requires DATABASE_URL/REDIS_URL to be present.
    from app.main import create_app

    provider_settings = _offline_provider_settings()
    adapter: LiveUniverseAdapter = OfflineFixtureProvider()
    dependency = LiveMarketRuntimeDependency(
        settings=provider_settings,
        error_threshold=provider_settings.strategy_error_threshold,
        adapter=adapter,
        clock=ManualClock(REFERENCE_INSTANT),
    )
    lifecycle = ApplicationLifecycle(
        InMemoryDatabaseLifecycle(), InMemoryRedisLifecycle(), provider=dependency
    )
    return create_app(lifecycle=lifecycle)
