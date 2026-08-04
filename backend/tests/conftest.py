"""Shared pytest fixtures.

Provides an HTTP client bound to the FastAPI app for endpoint tests. No
database or Redis is required by the Phase 1 smoke tests — those fixtures
will be added alongside the first persistence models.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

# Application assembly creates the configured client factories at import time.
# These non-secret values let endpoint tests assemble while configuration unit
# tests independently exercise missing and invalid runtime settings.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://apexscan:test-only-password@localhost:5432/apexscan",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app.main import create_app


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    """Yield an async HTTP client wired to the ASGI application."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
