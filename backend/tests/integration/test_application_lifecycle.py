"""Real PostgreSQL and Redis application-lifecycle tests, enabled by explicit opt-in."""

from __future__ import annotations

import os
from importlib import import_module
from types import ModuleType

import pytest
from httpx import ASGITransport, AsyncClient

from app.cache import RedisLifecycle
from app.core.config import get_settings
from app.database import DatabaseLifecycle
from app.main import create_app

pytestmark = pytest.mark.integration


def _lifecycle_module() -> ModuleType:
    """Load the P2.3 lifecycle boundary at test execution time."""
    return import_module("app.core.lifecycle")


@pytest.fixture
def integration_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure real service URLs only for an explicitly opted-in runner."""
    if os.getenv("APEXSCAN_RUN_INTEGRATION") != "1":
        pytest.skip("set APEXSCAN_RUN_INTEGRATION=1 to run real application lifecycle tests")

    database_url = os.getenv("INTEGRATION_DATABASE_URL") or os.environ["DATABASE_URL"]
    redis_url = os.getenv("INTEGRATION_REDIS_URL") or os.environ["REDIS_URL"]
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("REDIS_URL", redis_url)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_application_lifecycle_starts_health_endpoints_and_shuts_down_cleanly(
    integration_environment: None,
) -> None:
    """A real app reaches ready state with both stores, then releases them on exit."""
    lifecycle_module = _lifecycle_module()
    lifecycle = lifecycle_module.ApplicationLifecycle(DatabaseLifecycle(), RedisLifecycle())
    app = create_app(lifecycle=lifecycle)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/v1/health")).json() == {"status": "live"}
            assert (await client.get("/api/v1/health/ready")).status_code == 200
            assert (await client.get("/api/v1/health/startup")).json() == {"status": "started"}

    assert lifecycle.startup_snapshot().status == "stopped"


async def test_application_startup_is_blocked_when_postgresql_is_unavailable(
    integration_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real PostgreSQL connection failure prevents the application from serving."""
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://apexscan:unused@127.0.0.1:1/apexscan",
    )
    get_settings.cache_clear()
    lifecycle_module = _lifecycle_module()
    lifecycle = lifecycle_module.ApplicationLifecycle(DatabaseLifecycle(), RedisLifecycle())

    app = create_app(lifecycle=lifecycle)
    with pytest.raises(lifecycle_module.ApplicationStartupError):
        async with app.router.lifespan_context(app):
            pass

    assert lifecycle.startup_snapshot().status == "failed"


async def test_application_startup_is_blocked_when_redis_is_unavailable(
    integration_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real Redis connection failure prevents the application from serving."""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0?socket_connect_timeout=0.1")
    get_settings.cache_clear()
    lifecycle_module = _lifecycle_module()
    lifecycle = lifecycle_module.ApplicationLifecycle(DatabaseLifecycle(), RedisLifecycle())

    app = create_app(lifecycle=lifecycle)
    with pytest.raises(lifecycle_module.ApplicationStartupError):
        async with app.router.lifespan_context(app):
            pass

    assert lifecycle.startup_snapshot().status == "failed"
