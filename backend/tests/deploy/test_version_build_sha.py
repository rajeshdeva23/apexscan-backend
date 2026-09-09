"""Build-SHA propagation into settings and the /version endpoint (DEPLOY-1)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.config.settings import Settings


def test_build_sha_defaults_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUILD_SHA", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.build_sha == "unknown"


def test_build_sha_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUILD_SHA", "a" * 40)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.build_sha == "a" * 40


async def test_version_endpoint_exposes_build_sha(client: AsyncClient) -> None:
    response = await client.get("/api/v1/version")
    assert response.status_code == 200
    body = response.json()
    assert "build_sha" in body
    assert isinstance(body["build_sha"], str)
