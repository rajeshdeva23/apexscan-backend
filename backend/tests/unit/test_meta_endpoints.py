"""Smoke tests for the health and version endpoints.

Verifies the application assembles and the infrastructure endpoints respond
correctly — the behavioural contract of Phase 1.
"""

from __future__ import annotations

from httpx import AsyncClient


async def test_health_returns_ok(client: AsyncClient) -> None:
    """The health probe reports a live process."""
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_version_reports_metadata(client: AsyncClient) -> None:
    """The version endpoint returns app identity fields."""
    response = await client.get("/api/v1/version")
    assert response.status_code == 200
    body = response.json()
    assert {"name", "version", "environment"} <= body.keys()
