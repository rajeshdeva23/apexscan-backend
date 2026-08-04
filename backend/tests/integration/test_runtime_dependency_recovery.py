"""Real Compose-backed tests for runtime dependency outage and recovery."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.cache import RedisLifecycle
from app.core.config import get_settings
from app.core.lifecycle import ApplicationLifecycle
from app.database import DatabaseLifecycle
from app.main import create_app

pytestmark = pytest.mark.integration

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE_COMMAND_TIMEOUT_SECONDS = 30
_SERVICE_HEALTH_TIMEOUT_SECONDS = 60
_SERVICE_HEALTH_POLL_SECONDS = 1
_READINESS_ATTEMPTS = 30
_READINESS_POLL_SECONDS = 1


class _ComposeController:
    """Control the backend-owned Compose services for a real runtime test."""

    def _run(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "compose", *arguments],
            cwd=_REPOSITORY_ROOT,
            check=check,
            capture_output=True,
            text=True,
            timeout=_COMPOSE_COMMAND_TIMEOUT_SECONDS,
        )

    def stop(self, service: str) -> None:
        """Stop one dependency without stopping the application under test."""
        self._run("stop", service)

    def start(self, service: str) -> None:
        """Start one dependency and wait until Compose reports it healthy."""
        self._run("start", service)
        self.wait_for_healthy(service)

    def wait_for_healthy(self, service: str) -> None:
        """Wait on Compose's health state with a finite, diagnostic timeout."""
        deadline = time.monotonic() + _SERVICE_HEALTH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            container_id = self._run("ps", "-q", service).stdout.strip()
            if container_id:
                status = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Health.Status}}", container_id],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=_COMPOSE_COMMAND_TIMEOUT_SECONDS,
                ).stdout.strip()
                if status == "healthy":
                    return
            time.sleep(_SERVICE_HEALTH_POLL_SECONDS)

        raise AssertionError(f"{service} did not become healthy within the configured timeout")

    def restore_dependencies(self) -> None:
        """Best-effort cleanup so one failed scenario does not poison later CI steps."""
        self._run("start", "postgres", "redis", check=False)


@pytest.fixture
def compose_runtime_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[_ComposeController]:
    """Provide real host-reachable dependency URLs to explicit Compose runners only."""
    if os.getenv("APEXSCAN_RUN_INTEGRATION") != "1":
        pytest.skip("set APEXSCAN_RUN_INTEGRATION=1 to run Compose runtime tests")
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for Compose runtime tests")

    database_url = os.getenv("INTEGRATION_DATABASE_URL")
    redis_url = os.getenv("INTEGRATION_REDIS_URL")
    if database_url is None or redis_url is None:
        pytest.skip(
            "set INTEGRATION_DATABASE_URL and INTEGRATION_REDIS_URL for Compose runtime tests"
        )

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("REDIS_URL", redis_url)
    get_settings.cache_clear()
    controller = _ComposeController()
    try:
        yield controller
    finally:
        controller.restore_dependencies()
        get_settings.cache_clear()


async def _wait_for_status(
    client: AsyncClient,
    path: str,
    expected_status: int,
) -> dict[str, object]:
    """Poll an operational endpoint until its expected HTTP state appears."""
    for _ in range(_READINESS_ATTEMPTS):
        response = await client.get(path)
        if response.status_code == expected_status:
            return response.json()
        await asyncio.sleep(_READINESS_POLL_SECONDS)

    raise AssertionError(
        f"{path} did not return HTTP {expected_status} within the configured timeout"
    )


async def test_real_application_recovers_readiness_after_postgresql_and_redis_outages(
    compose_runtime_environment: _ComposeController,
) -> None:
    """A real running app stays live and returns ready after each Compose dependency recovers."""
    database = DatabaseLifecycle()
    redis = RedisLifecycle()
    lifecycle = ApplicationLifecycle(database, redis)
    app = create_app(lifecycle=lifecycle)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await _wait_for_status(client, "/api/v1/health/ready", 200))[
                "status"
            ] == "ready"

            compose_runtime_environment.stop("postgres")
            postgres_outage = await _wait_for_status(client, "/api/v1/health/ready", 503)
            assert postgres_outage["dependencies"] == {"database": "unhealthy", "redis": "healthy"}
            assert (await client.get("/api/v1/health")).json() == {"status": "live"}

            compose_runtime_environment.start("postgres")
            assert (await _wait_for_status(client, "/api/v1/health/ready", 200))[
                "status"
            ] == "ready"

            compose_runtime_environment.stop("redis")
            redis_outage = await _wait_for_status(client, "/api/v1/health/ready", 503)
            assert redis_outage["dependencies"] == {"database": "healthy", "redis": "unhealthy"}
            assert (await client.get("/api/v1/health")).json() == {"status": "live"}

            compose_runtime_environment.start("redis")
            assert (await _wait_for_status(client, "/api/v1/health/ready", 200))[
                "status"
            ] == "ready"

    assert lifecycle.startup_snapshot().status == "stopped"
    assert database.is_initialized is False
    assert redis.is_initialized is False
