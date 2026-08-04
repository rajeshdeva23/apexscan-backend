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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.cache import redis_pool
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database import engine
from app.middleware.request_logging import RequestLoggingMiddleware

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Manage application startup and shutdown.

    Startup logs readiness; shutdown disposes the database engine and Redis
    pool so connections are released cleanly. Resource creation itself lives
    in the database/cache modules — this only owns lifecycle.
    """
    settings = get_settings()
    logger.info("Starting %s (%s)", settings.app_name, settings.app_env)
    yield
    logger.info("Shutting down %s", settings.app_name)
    await engine.dispose()
    await redis_pool.aclose()


def create_app() -> FastAPI:
    """Build and configure the FastAPI application.

    Returns:
        A fully wired :class:`FastAPI` instance ready to serve requests.
    """
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.app_debug,
        lifespan=lifespan,
    )

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
