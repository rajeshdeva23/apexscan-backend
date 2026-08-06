"""Tests for provider lifecycle coordination and readiness integration."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.lifecycle import (
    ApplicationLifecycle,
    ApplicationShutdownError,
    ApplicationStartupError,
)
from app.schemas.market_data import ProviderStatus
from tests.fakes.controlled_provider_adapter import ControlledProviderAdapter

_DATABASE_URL = "postgresql+asyncpg://apexscan:unit-test-password@postgres:5432/apexscan"
_REDIS_URL = "redis://redis:6379/0"


class _Database:
    """Test PostgreSQL boundary that records lifecycle order without external I/O."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def initialize(self, _url: str, *, echo: bool = False) -> None:
        assert echo is False
        self.events.append("database.initialize")

    async def verify_connectivity(self) -> None:
        self.events.append("database.verify")

    async def dispose(self) -> None:
        self.events.append("database.dispose")


class _Redis:
    """Test Redis boundary that records lifecycle order without external I/O."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def initialize(self, _url: str) -> None:
        self.events.append("redis.initialize")

    async def verify_connectivity(self) -> None:
        self.events.append("redis.verify")

    async def close(self) -> None:
        self.events.append("redis.close")


def _provider_module() -> ModuleType:
    """Import the P3.2 coordinator only after the test declares its contract."""
    try:
        return import_module("app.adapters.base.provider_coordinator")
    except ModuleNotFoundError:
        pytest.fail("P3.2 must provide broker-independent provider lifecycle coordination")


def _settings() -> Settings:
    """Create settings with a short, deterministic provider operation timeout."""
    return Settings(
        database_url=_DATABASE_URL,
        redis_url=_REDIS_URL,
        app_debug=False,
        provider_lifecycle_timeout_seconds=0.01,
    )


async def _get(app: FastAPI, path: str) -> tuple[int, dict[str, object]]:
    """Call a health endpoint without invoking the app lifespan a second time."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)
    return response.status_code, response.json()


async def test_provider_coordinator_connects_once_probes_and_disconnects_idempotently() -> None:
    """A coordinator owns one adapter lifecycle without broker-specific behavior."""
    events: list[str] = []
    provider_module = _provider_module()
    adapter = ControlledProviderAdapter(events)
    coordinator = provider_module.ProviderCoordinator(adapter)

    await coordinator.start(timeout_seconds=0.01)
    await coordinator.start(timeout_seconds=0.01)
    health = await coordinator.verify_health()
    await coordinator.shutdown()
    await coordinator.shutdown()

    assert health.status is ProviderStatus.HEALTHY
    assert adapter.connect_calls == 1
    assert adapter.disconnect_calls == 1
    assert events == [
        "provider.connect",
        "provider.health",
        "provider.health",
        "provider.disconnect",
    ]


async def test_provider_connection_failure_blocks_startup_and_cleans_dependencies_safely() -> None:
    """A provider failure aborts readiness and cleans up in reverse dependency order."""
    events: list[str] = []
    provider_module = _provider_module()
    adapter = ControlledProviderAdapter(events)
    secret = "never-expose-provider-connect-secret"
    adapter.connect_error = RuntimeError(secret)
    lifecycle = ApplicationLifecycle(
        _Database(events),
        _Redis(events),
        provider_module.ProviderCoordinator(adapter),
    )

    with pytest.raises(ApplicationStartupError) as captured:
        await lifecycle.start(_settings())

    assert events == [
        "database.initialize",
        "database.verify",
        "redis.initialize",
        "redis.verify",
        "provider.connect",
        "provider.disconnect",
        "redis.close",
        "database.dispose",
    ]
    assert lifecycle.startup_snapshot().status == "failed"
    assert secret not in str(captured.value)


async def test_unhealthy_required_provider_blocks_startup_before_application_is_ready() -> None:
    """A connected but unhealthy provider cannot satisfy mandatory startup health."""
    events: list[str] = []
    provider_module = _provider_module()
    adapter = ControlledProviderAdapter(events)
    adapter.health_status = ProviderStatus.DOWN
    lifecycle = ApplicationLifecycle(
        _Database(events),
        _Redis(events),
        provider_module.ProviderCoordinator(adapter),
    )

    with pytest.raises(ApplicationStartupError):
        await lifecycle.start(_settings())

    assert lifecycle.startup_snapshot().status == "failed"
    assert (await lifecycle.readiness_snapshot()).status == "not_ready"
    assert events[-3:] == ["provider.disconnect", "redis.close", "database.dispose"]


async def test_unconfigured_provider_blocks_startup_without_requiring_provider_credentials() -> (
    None
):
    """The composition root fails safely until a future concrete adapter is injected."""
    events: list[str] = []
    provider_module = _provider_module()
    lifecycle = ApplicationLifecycle(
        _Database(events),
        _Redis(events),
        provider_module.ProviderCoordinator(),
    )

    with pytest.raises(ApplicationStartupError) as captured:
        await lifecycle.start(_settings())

    assert lifecycle.startup_snapshot().status == "failed"
    assert "credential" not in str(captured.value).lower()
    assert events == [
        "database.initialize",
        "database.verify",
        "redis.initialize",
        "redis.verify",
        "redis.close",
        "database.dispose",
    ]


async def test_provider_health_controls_readiness_but_not_liveness_and_recovers() -> None:
    """A degraded or down provider removes readiness until a healthy probe recovers it."""
    events: list[str] = []
    provider_module = _provider_module()
    adapter = ControlledProviderAdapter(events)
    lifecycle = ApplicationLifecycle(
        _Database(events),
        _Redis(events),
        provider_module.ProviderCoordinator(adapter),
    )
    await lifecycle.start(_settings())

    adapter.health_status = ProviderStatus.DEGRADED
    degraded = await lifecycle.readiness_snapshot()
    assert degraded.as_dict() == {
        "status": "not_ready",
        "startup": "started",
        "dependencies": {
            "database": "healthy",
            "redis": "healthy",
            "provider": "unhealthy",
        },
    }
    assert lifecycle.liveness_snapshot().as_dict() == {"status": "live"}

    adapter.health_status = ProviderStatus.DOWN
    assert (await lifecycle.readiness_snapshot()).status == "not_ready"

    adapter.health_status = ProviderStatus.HEALTHY
    assert (await lifecycle.readiness_snapshot()).as_dict()["status"] == "ready"


async def test_provider_health_exception_is_redacted_from_runtime_readiness() -> None:
    """Provider probe diagnostics do not escape the dependency readiness boundary."""
    events: list[str] = []
    provider_module = _provider_module()
    adapter = ControlledProviderAdapter(events)
    lifecycle = ApplicationLifecycle(
        _Database(events),
        _Redis(events),
        provider_module.ProviderCoordinator(adapter),
    )
    await lifecycle.start(_settings())
    secret = "never-expose-provider-health-secret"
    adapter.health_error = RuntimeError(secret)

    readiness = (await lifecycle.readiness_snapshot()).as_dict()

    assert readiness["status"] == "not_ready"
    assert readiness["dependencies"] == {
        "database": "healthy",
        "redis": "healthy",
        "provider": "unhealthy",
    }
    assert secret not in str(readiness)
    assert lifecycle.liveness_snapshot().as_dict() == {"status": "live"}


async def test_provider_timeout_is_translated_without_exposing_adapter_diagnostics() -> None:
    """A blocked provider operation is bounded and surfaces only a safe timeout error."""
    events: list[str] = []
    provider_module = _provider_module()
    adapter = ControlledProviderAdapter(events)
    adapter.block_connect = True
    coordinator = provider_module.ProviderCoordinator(adapter)

    with pytest.raises(provider_module.ProviderOperationTimeoutError) as captured:
        await coordinator.start(timeout_seconds=0.001)

    assert "provider.connect" not in str(captured.value).lower()
    await coordinator.shutdown()
    assert adapter.disconnect_calls == 1


async def test_provider_disconnect_failure_does_not_block_redis_or_database_cleanup() -> None:
    """Ordered cleanup continues when provider disconnect reports a safe failure."""
    events: list[str] = []
    provider_module = _provider_module()
    adapter = ControlledProviderAdapter(events)
    secret = "never-expose-provider-disconnect-secret"
    adapter.disconnect_error = RuntimeError(secret)
    lifecycle = ApplicationLifecycle(
        _Database(events),
        _Redis(events),
        provider_module.ProviderCoordinator(adapter),
    )
    await lifecycle.start(_settings())
    events.clear()

    with pytest.raises(ApplicationShutdownError) as captured:
        await lifecycle.shutdown()

    assert events == ["provider.disconnect", "redis.close", "database.dispose"]
    assert secret not in str(captured.value)


async def test_readiness_endpoint_reports_provider_outage_and_recovery_without_new_api() -> None:
    """Existing health endpoints expose provider state while liveness remains process-only."""
    main_module: Any = import_module("app.main")
    events: list[str] = []
    provider_module = _provider_module()
    adapter = ControlledProviderAdapter(events)
    lifecycle = ApplicationLifecycle(
        _Database(events),
        _Redis(events),
        provider_module.ProviderCoordinator(adapter),
    )
    await lifecycle.start(_settings())
    app = main_module.create_app(lifecycle=lifecycle)

    assert await _get(app, "/api/v1/health/ready") == (
        200,
        {
            "status": "ready",
            "startup": "started",
            "dependencies": {
                "database": "healthy",
                "redis": "healthy",
                "provider": "healthy",
            },
        },
    )

    adapter.health_status = ProviderStatus.DOWN
    assert (await _get(app, "/api/v1/health/ready"))[0] == 503
    assert await _get(app, "/api/v1/health") == (200, {"status": "live"})

    adapter.health_status = ProviderStatus.HEALTHY
    assert (await _get(app, "/api/v1/health/ready"))[0] == 200
