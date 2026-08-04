"""API regression tests for runtime dependency health transitions."""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.lifecycle import ApplicationLifecycle
from app.main import create_app

_DATABASE_URL = "postgresql+asyncpg://apexscan:api-test-password@postgres:5432/apexscan"
_REDIS_URL = "redis://redis:6379/0"


class _HealthDatabase:
    """A test-only PostgreSQL boundary with an explicit runtime availability flag."""

    def __init__(self) -> None:
        self.is_available = True

    async def initialize(self, _url: str, *, echo: bool = False) -> None:
        assert echo is False

    async def verify_connectivity(self) -> None:
        if not self.is_available:
            raise RuntimeError("database connection used api-test-secret")

    async def dispose(self) -> None:
        return None


class _HealthRedis:
    """A test-only Redis boundary with an explicit runtime availability flag."""

    def __init__(self) -> None:
        self.is_available = True

    async def initialize(self, _url: str) -> None:
        return None

    async def verify_connectivity(self) -> None:
        if not self.is_available:
            raise RuntimeError("redis connection used api-test-secret")

    async def close(self) -> None:
        return None


def _settings() -> Settings:
    """Create valid typed settings without accessing local environment state."""
    return Settings(database_url=_DATABASE_URL, redis_url=_REDIS_URL, app_debug=False)


async def _get(app: FastAPI, path: str) -> tuple[int, dict[str, object]]:
    """Request a health endpoint without running the FastAPI lifespan hook."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)
    return response.status_code, response.json()


async def test_health_endpoints_keep_liveness_during_outages_and_restore_readiness() -> None:
    """On-demand probes must expose outage and recovery without process restart."""
    database = _HealthDatabase()
    redis = _HealthRedis()
    lifecycle = ApplicationLifecycle(database, redis)
    await lifecycle.start(_settings())
    app = create_app(lifecycle=lifecycle)

    database.is_available = False
    postgres_status, postgres_body = await _get(app, "/api/v1/health/ready")
    assert postgres_status == 503
    assert postgres_body["dependencies"] == {"database": "unhealthy", "redis": "healthy"}
    assert await _get(app, "/api/v1/health") == (200, {"status": "live"})

    database.is_available = True
    assert await _get(app, "/api/v1/health/ready") == (
        200,
        {
            "status": "ready",
            "startup": "started",
            "dependencies": {"database": "healthy", "redis": "healthy"},
        },
    )

    redis.is_available = False
    redis_status, redis_body = await _get(app, "/api/v1/health/ready")
    assert redis_status == 503
    assert redis_body["dependencies"] == {"database": "healthy", "redis": "unhealthy"}
    assert await _get(app, "/api/v1/health") == (200, {"status": "live"})

    redis.is_available = True
    assert (await _get(app, "/api/v1/health/ready"))[0] == 200


async def test_readiness_reports_both_outages_without_sensitive_diagnostics() -> None:
    """The API must report both unavailable stores while redacting probe failures."""
    database = _HealthDatabase()
    redis = _HealthRedis()
    lifecycle = ApplicationLifecycle(database, redis)
    await lifecycle.start(_settings())
    app = create_app(lifecycle=lifecycle)
    database.is_available = False
    redis.is_available = False

    status_code, body = await _get(app, "/api/v1/health/ready")

    assert status_code == 503
    assert body == {
        "status": "not_ready",
        "startup": "started",
        "dependencies": {"database": "unhealthy", "redis": "unhealthy"},
    }
    assert "api-test-secret" not in str(body)
