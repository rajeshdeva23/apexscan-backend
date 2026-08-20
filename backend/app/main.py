"""ApexScan FastAPI application entry point.

Composition root: builds the ASGI application, configures logging, CORS, and
middleware, wires the versioned API router, and manages startup/shutdown of
shared resources (database engine, Redis pool) via the lifespan handler.

No business logic lives here — only application assembly.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.cache import redis_lifecycle
from app.core.config import Settings, get_settings
from app.core.lifecycle import (
    ApplicationLifecycle,
    ApplicationShutdownError,
    ApplicationStartupError,
    ProviderDependency,
)
from app.core.logging import configure_logging
from app.database import database_lifecycle
from app.middleware.request_logging import RequestLoggingMiddleware
from app.services.dhan_runtime_composition import LiveMarketRuntimeDependency

logger = logging.getLogger(__name__)


def _provider_dependency(settings: Settings) -> ProviderDependency | None:
    """Build the live-market runtime dependency when the provider is explicitly enabled.

    Construction is side-effect free — provider I/O happens inside the lifespan via
    ``ApplicationLifecycle.start`` → the dependency's ``start`` (ADR-010 D8/D14). When the
    provider is disabled, the app runs dormant (``None``) with no broker credentials.
    """
    if not settings.market_provider_enabled:
        return None
    return LiveMarketRuntimeDependency(
        settings=settings, error_threshold=settings.strategy_error_threshold
    )


def _build_application_lifecycle(settings: Settings) -> ApplicationLifecycle:
    """Assemble the application lifecycle over the shared DB/Redis + optional runtime."""
    return ApplicationLifecycle(
        database_lifecycle, redis_lifecycle, provider=_provider_dependency(settings)
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Manage application startup and shutdown.

    Startup gates traffic on PostgreSQL then Redis verification. Shutdown first
    removes readiness, then releases Redis and PostgreSQL in reverse order.
    """
    settings = get_settings()
    lifecycle = cast(ApplicationLifecycle, app.state.lifecycle)
    logger.info("Starting %s (%s)", settings.app_name, settings.app_env)
    try:
        await lifecycle.start(settings)
    except ApplicationStartupError:
        logger.error("Application startup blocked by a mandatory dependency")
        raise

    try:
        yield
    finally:
        logger.info("Shutting down %s", settings.app_name)
        try:
            await lifecycle.shutdown()
        except ApplicationShutdownError:
            logger.error("Application shutdown dependency cleanup failed")
            raise


def create_app(lifecycle: ApplicationLifecycle | None = None) -> FastAPI:
    """Build and configure the FastAPI application.

    Returns:
        A fully wired :class:`FastAPI` instance with an injected lifecycle owner.
    """
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.app_debug,
        lifespan=lifespan,
    )
    app.state.lifecycle = lifecycle or _build_application_lifecycle(settings)

    # Cross-origin access for the browser frontend.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Structured per-request access logging.
    app.add_middleware(RequestLoggingMiddleware)

    # Versioned API surface.
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    return app


# ASGI entry point referenced by uvicorn (``app.main:app``).
app = create_app()
