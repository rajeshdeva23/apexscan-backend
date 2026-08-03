"""Shared pytest fixtures.

Provides an HTTP client bound to the FastAPI app for endpoint tests. No
database or Redis is required by the Phase 1 smoke tests — those fixtures
will be added alongside the first persistence models.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Yield an async HTTP client wired to the ASGI application."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
