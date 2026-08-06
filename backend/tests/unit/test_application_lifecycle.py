"""Unit tests for application startup, shutdown, and readiness state."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings

_DATABASE_URL = "postgresql+asyncpg://apexscan:unit-test-password@postgres:5432/apexscan"
_REDIS_URL = "redis://redis:6379/0"


class _FakeDatabase:
    """External PostgreSQL lifecycle boundary with observable operations."""

    def __init__(self, events: list[str]) -> None:
        self.initialize = AsyncMock(side_effect=self._initialize)
        self.verify_connectivity = AsyncMock(side_effect=self._verify_connectivity)
        self.dispose = AsyncMock(side_effect=self._dispose)
        self._events = events

    async def _initialize(self, _url: str, *, echo: bool) -> None:
        assert echo is False
        self._events.append("database.initialize")

    async def _verify_connectivity(self) -> None:
        self._events.append("database.verify")

    async def _dispose(self) -> None:
        self._events.append("database.dispose")


class _FakeRedis:
    """External Redis lifecycle boundary with observable operations."""

    def __init__(self, events: list[str]) -> None:
        self.initialize = AsyncMock(side_effect=self._initialize)
        self.verify_connectivity = AsyncMock(side_effect=self._verify_connectivity)
        self.close = AsyncMock(side_effect=self._close)
        self._events = events

    async def _initialize(self, _url: str) -> None:
        self._events.append("redis.initialize")

    async def _verify_connectivity(self) -> None:
        self._events.append("redis.verify")

    async def _close(self) -> None:
        self._events.append("redis.close")


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


async def test_startup_orders_mandatory_dependency_initialization_and_probes() -> None:
    """Startup reaches ready only after PostgreSQL then Redis have verified successfully."""
    events: list[str] = []
    lifecycle_module = _lifecycle_module()
    lifecycle = lifecycle_module.ApplicationLifecycle(_FakeDatabase(events), _FakeRedis(events))

    await lifecycle.start(_settings())

    assert events == [
        "database.initialize",
        "database.verify",
        "redis.initialize",
        "redis.verify",
    ]
    assert lifecycle.startup_snapshot().status == "started"
    assert (await lifecycle.readiness_snapshot()).status == "ready"


async def test_partial_startup_failure_cleans_redis_then_postgresql_without_secret_leakage() -> (
    None
):
    """A Redis initialization failure blocks startup and safely releases earlier resources."""
    events: list[str] = []
    secret = "must-not-appear-in-startup-error"
    database = _FakeDatabase(events)
    redis = _FakeRedis(events)
    redis.initialize.side_effect = RuntimeError(f"failed for {secret}")
    lifecycle_module = _lifecycle_module()
    lifecycle = lifecycle_module.ApplicationLifecycle(database, redis)

    with pytest.raises(lifecycle_module.ApplicationStartupError) as captured:
        await lifecycle.start(_settings())

    assert events == [
        "database.initialize",
        "database.verify",
        "redis.close",
        "database.dispose",
    ]
    assert lifecycle.startup_snapshot().status == "failed"
    assert (await lifecycle.readiness_snapshot()).status == "not_ready"
    assert secret not in str(captured.value)


async def test_readiness_reprobes_dependencies_without_changing_liveness() -> None:
    """A failed on-demand probe removes readiness while the process remains live."""
    events: list[str] = []
    database = _FakeDatabase(events)
    redis = _FakeRedis(events)
    lifecycle_module = _lifecycle_module()
    lifecycle = lifecycle_module.ApplicationLifecycle(database, redis)

    await lifecycle.start(_settings())
    database.verify_connectivity.side_effect = RuntimeError("database is unavailable")

    readiness = await lifecycle.readiness_snapshot()

    assert readiness.status == "not_ready"
    assert readiness.dependencies["database"] == "unhealthy"
    assert readiness.dependencies["redis"] == "healthy"
    assert lifecycle.liveness_snapshot().status == "live"


async def test_shutdown_stops_readiness_before_idempotent_reverse_resource_cleanup() -> None:
    """Shutdown prevents new readiness and closes Redis before disposing PostgreSQL once."""
    events: list[str] = []
    database = _FakeDatabase(events)
    redis = _FakeRedis(events)
    lifecycle_module = _lifecycle_module()
    lifecycle = lifecycle_module.ApplicationLifecycle(database, redis)

    await lifecycle.start(_settings())
    events.clear()
    await lifecycle.shutdown()
    await lifecycle.shutdown()

    assert events == ["redis.close", "database.dispose"]
    assert lifecycle.startup_snapshot().status == "stopped"
    assert (await lifecycle.readiness_snapshot()).status == "not_ready"


async def test_startup_snapshot_reports_initialization_in_progress() -> None:
    """An unstarted process is live but neither startup-complete nor ready."""
    lifecycle_module = _lifecycle_module()
    lifecycle = lifecycle_module.ApplicationLifecycle(_FakeDatabase([]), _FakeRedis([]))

    assert lifecycle.liveness_snapshot().status == "live"
    assert lifecycle.startup_snapshot().status == "starting"
    assert (await lifecycle.readiness_snapshot()).status == "not_ready"


def test_composition_root_registers_no_unconfigured_provider() -> None:
    """Regression (C1): the shipped lifecycle must not carry a provider that blocks startup."""
    main_module = import_module("app.main")

    assert main_module.application_lifecycle._provider is None


async def test_lifecycle_wired_like_composition_root_reaches_ready() -> None:
    """The composition-root wiring (database + redis only) starts and reports ready."""
    lifecycle_module = _lifecycle_module()
    lifecycle = lifecycle_module.ApplicationLifecycle(_FakeDatabase([]), _FakeRedis([]))

    await lifecycle.start(_settings())
    snapshot = await lifecycle.readiness_snapshot()
    await lifecycle.shutdown()

    assert snapshot.status == "ready"
    assert "provider" not in (snapshot.dependencies or {})


async def test_bare_provider_coordinator_wiring_would_block_startup() -> None:
    """A ProviderCoordinator with no adapter is not a startable mandatory dependency."""
    coordinator_module = import_module("app.adapters.base")
    lifecycle_module = _lifecycle_module()
    lifecycle = lifecycle_module.ApplicationLifecycle(
        _FakeDatabase([]),
        _FakeRedis([]),
        coordinator_module.ProviderCoordinator(),
    )

    with pytest.raises(lifecycle_module.ApplicationStartupError):
        await lifecycle.start(_settings())
