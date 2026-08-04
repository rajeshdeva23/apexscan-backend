"""Regression tests for runtime dependency loss, recovery, and shutdown."""

from __future__ import annotations

from app.core.config import Settings
from app.core.lifecycle import ApplicationLifecycle

_DATABASE_URL = "postgresql+asyncpg://apexscan:unit-test-password@postgres:5432/apexscan"
_REDIS_URL = "redis://redis:6379/0"


class _ControllableDatabase:
    """A PostgreSQL boundary whose availability can change during a test."""

    def __init__(self, events: list[str]) -> None:
        self.is_available = True
        self.events = events

    async def initialize(self, _url: str, *, echo: bool = False) -> None:
        assert echo is False
        self.events.append("database.initialize")

    async def verify_connectivity(self) -> None:
        if not self.is_available:
            raise RuntimeError("database unavailable for unit-test-secret")
        self.events.append("database.verify")

    async def dispose(self) -> None:
        self.events.append("database.dispose")


class _ControllableRedis:
    """A Redis boundary whose availability can change during a test."""

    def __init__(self, events: list[str]) -> None:
        self.is_available = True
        self.events = events

    async def initialize(self, _url: str) -> None:
        self.events.append("redis.initialize")

    async def verify_connectivity(self) -> None:
        if not self.is_available:
            raise RuntimeError("redis unavailable for unit-test-secret")
        self.events.append("redis.verify")

    async def close(self) -> None:
        self.events.append("redis.close")


class _ReadinessObservingRedis(_ControllableRedis):
    """Capture readiness from within teardown to prove the transition order."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.lifecycle: ApplicationLifecycle | None = None
        self.readiness_during_close: str | None = None

    async def close(self) -> None:
        assert self.lifecycle is not None
        self.readiness_during_close = (await self.lifecycle.readiness_snapshot()).status
        await super().close()


def _settings() -> Settings:
    """Create valid settings without using developer-local environment state."""
    return Settings(database_url=_DATABASE_URL, redis_url=_REDIS_URL, app_debug=False)


async def test_postgresql_outage_removes_readiness_without_stopping_the_process() -> None:
    """A PostgreSQL probe failure must not turn a live process into a dead one."""
    events: list[str] = []
    database = _ControllableDatabase(events)
    lifecycle = ApplicationLifecycle(database, _ControllableRedis(events))
    await lifecycle.start(_settings())

    database.is_available = False
    readiness = await lifecycle.readiness_snapshot()

    assert readiness.as_dict() == {
        "status": "not_ready",
        "startup": "started",
        "dependencies": {"database": "unhealthy", "redis": "healthy"},
    }
    assert lifecycle.liveness_snapshot().as_dict() == {"status": "live"}


async def test_postgresql_recovery_restores_readiness_without_an_application_restart() -> None:
    """A healthy PostgreSQL probe after an outage must restore readiness in place."""
    events: list[str] = []
    database = _ControllableDatabase(events)
    lifecycle = ApplicationLifecycle(database, _ControllableRedis(events))
    await lifecycle.start(_settings())

    database.is_available = False
    assert (await lifecycle.readiness_snapshot()).status == "not_ready"
    database.is_available = True

    assert (await lifecycle.readiness_snapshot()).as_dict() == {
        "status": "ready",
        "startup": "started",
        "dependencies": {"database": "healthy", "redis": "healthy"},
    }
    assert lifecycle.startup_snapshot().status == "started"


async def test_redis_outage_removes_readiness_without_stopping_the_process() -> None:
    """A Redis probe failure must leave liveness independent of cache availability."""
    events: list[str] = []
    redis = _ControllableRedis(events)
    lifecycle = ApplicationLifecycle(_ControllableDatabase(events), redis)
    await lifecycle.start(_settings())

    redis.is_available = False
    readiness = await lifecycle.readiness_snapshot()

    assert readiness.as_dict() == {
        "status": "not_ready",
        "startup": "started",
        "dependencies": {"database": "healthy", "redis": "unhealthy"},
    }
    assert lifecycle.liveness_snapshot().as_dict() == {"status": "live"}


async def test_redis_recovery_restores_readiness_without_an_application_restart() -> None:
    """A healthy Redis probe after an outage must restore readiness in place."""
    events: list[str] = []
    redis = _ControllableRedis(events)
    lifecycle = ApplicationLifecycle(_ControllableDatabase(events), redis)
    await lifecycle.start(_settings())

    redis.is_available = False
    assert (await lifecycle.readiness_snapshot()).status == "not_ready"
    redis.is_available = True

    assert (await lifecycle.readiness_snapshot()).status == "ready"
    assert lifecycle.startup_snapshot().status == "started"


async def test_simultaneous_dependency_outages_report_both_as_unhealthy() -> None:
    """Readiness must identify each failed mandatory dependency without diagnostics."""
    events: list[str] = []
    database = _ControllableDatabase(events)
    redis = _ControllableRedis(events)
    lifecycle = ApplicationLifecycle(database, redis)
    await lifecycle.start(_settings())

    database.is_available = False
    redis.is_available = False

    assert (await lifecycle.readiness_snapshot()).as_dict() == {
        "status": "not_ready",
        "startup": "started",
        "dependencies": {"database": "unhealthy", "redis": "unhealthy"},
    }


async def test_shutdown_removes_readiness_before_dependency_teardown() -> None:
    """Teardown must observe unavailable readiness before closing either dependency."""
    events: list[str] = []
    redis = _ReadinessObservingRedis(events)
    lifecycle = ApplicationLifecycle(_ControllableDatabase(events), redis)
    redis.lifecycle = lifecycle
    await lifecycle.start(_settings())
    events.clear()

    await lifecycle.shutdown()

    assert redis.readiness_during_close == "not_ready"
    assert events == ["redis.close", "database.dispose"]
    assert lifecycle.startup_snapshot().status == "stopped"


async def test_shutdown_cleans_resources_while_a_dependency_is_unavailable() -> None:
    """An outage must not prevent reverse-order resource cleanup during shutdown."""
    events: list[str] = []
    database = _ControllableDatabase(events)
    lifecycle = ApplicationLifecycle(database, _ControllableRedis(events))
    await lifecycle.start(_settings())
    database.is_available = False
    assert (await lifecycle.readiness_snapshot()).status == "not_ready"
    events.clear()

    await lifecycle.shutdown()

    assert events == ["redis.close", "database.dispose"]
    assert lifecycle.startup_snapshot().status == "stopped"
