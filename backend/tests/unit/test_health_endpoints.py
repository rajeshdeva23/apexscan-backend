"""API tests for truthful liveness, readiness, and startup health responses."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app

_DATABASE_URL = "postgresql+asyncpg://apexscan:api-test-password@postgres:5432/apexscan"
_REDIS_URL = "redis://redis:6379/0"


class _HealthyDatabase:
    """PostgreSQL lifecycle boundary used by API health tests."""

    def __init__(self) -> None:
        self.initialize = AsyncMock()
        self.verify_connectivity = AsyncMock()
        self.dispose = AsyncMock()


class _HealthyRedis:
    """Redis lifecycle boundary used by API health tests."""

    def __init__(self) -> None:
        self.initialize = AsyncMock()
        self.verify_connectivity = AsyncMock()
        self.close = AsyncMock()


def _lifecycle_module() -> ModuleType:
    """Load the P2.3 lifecycle boundary at test execution time."""
    return import_module("app.core.lifecycle")


def _settings() -> Settings:
    """Create valid typed settings without reading local developer state."""
    return Settings(
        database_url=_DATABASE_URL,
        redis_url=_REDIS_URL,
        app_debug=False,
    )


async def _get(app_path: str, app: object) -> tuple[int, dict[str, object]]:
    """Request one health endpoint without running FastAPI lifespan hooks."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(app_path)
    return response.status_code, response.json()


async def test_liveness_is_available_before_dependency_startup() -> None:
    """Liveness reports a functioning process even while it is not ready for traffic."""
    lifecycle_module = _lifecycle_module()
    lifecycle = lifecycle_module.ApplicationLifecycle(_HealthyDatabase(), _HealthyRedis())
    app = create_app(lifecycle=lifecycle)

    status_code, body = await _get("/api/v1/health", app)

    assert status_code == 200
    assert body == {"status": "live"}


async def test_readiness_returns_success_after_mandatory_dependencies_start() -> None:
    """Readiness reports ready only after both mandatory dependencies have started."""
    lifecycle_module = _lifecycle_module()
    lifecycle = lifecycle_module.ApplicationLifecycle(_HealthyDatabase(), _HealthyRedis())
    await lifecycle.start(_settings())
    app = create_app(lifecycle=lifecycle)

    status_code, body = await _get("/api/v1/health/ready", app)

    assert status_code == 200
    assert body == {
        "status": "ready",
        "startup": "started",
        "dependencies": {"database": "healthy", "redis": "healthy"},
    }


async def test_readiness_returns_service_unavailable_without_sensitive_probe_detail() -> None:
    """A failed dependency probe returns 503 state only, never the underlying error text."""
    secret = "must-not-appear-in-readiness-response"
    database = _HealthyDatabase()
    redis = _HealthyRedis()
    lifecycle_module = _lifecycle_module()
    lifecycle = lifecycle_module.ApplicationLifecycle(database, redis)
    await lifecycle.start(_settings())
    database.verify_connectivity.side_effect = RuntimeError(f"failed for {secret}")
    app = create_app(lifecycle=lifecycle)

    status_code, body = await _get("/api/v1/health/ready", app)

    assert status_code == 503
    assert body == {
        "status": "not_ready",
        "startup": "started",
        "dependencies": {"database": "unhealthy", "redis": "healthy"},
    }
    assert secret not in str(body)


async def test_startup_endpoint_distinguishes_in_progress_failure_and_success() -> None:
    """Startup state is separate from readiness and reports only safe lifecycle labels."""
    lifecycle_module = _lifecycle_module()
    lifecycle = lifecycle_module.ApplicationLifecycle(_HealthyDatabase(), _HealthyRedis())
    app = create_app(lifecycle=lifecycle)

    initial_status, initial_body = await _get("/api/v1/health/startup", app)
    assert initial_status == 503
    assert initial_body == {"status": "starting"}

    await lifecycle.start(_settings())
    started_status, started_body = await _get("/api/v1/health/startup", app)
    assert started_status == 200
    assert started_body == {"status": "started"}

    failed_database = _HealthyDatabase()
    failed_database.initialize.side_effect = RuntimeError("database unavailable")
    failed_lifecycle = lifecycle_module.ApplicationLifecycle(failed_database, _HealthyRedis())
    with pytest.raises(lifecycle_module.ApplicationStartupError):
        await failed_lifecycle.start(_settings())
    failed_app = create_app(lifecycle=failed_lifecycle)

    failed_status, failed_body = await _get("/api/v1/health/startup", failed_app)
    assert failed_status == 503
    assert failed_body == {"status": "failed"}


async def test_lifespan_blocks_startup_when_a_mandatory_dependency_fails() -> None:
    """The application lifespan never reaches serving state when PostgreSQL cannot start."""
    lifecycle_module = _lifecycle_module()
    database = _HealthyDatabase()
    database.initialize.side_effect = RuntimeError("database unavailable")
    lifecycle = lifecycle_module.ApplicationLifecycle(database, _HealthyRedis())
    app = create_app(lifecycle=lifecycle)

    with pytest.raises(lifecycle_module.ApplicationStartupError):
        async with app.router.lifespan_context(app):
            pass

    assert lifecycle.startup_snapshot().status == "failed"
