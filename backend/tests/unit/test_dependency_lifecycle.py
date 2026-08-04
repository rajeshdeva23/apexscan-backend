"""Unit tests for managed PostgreSQL and Redis dependency lifecycles."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

import app.cache.redis_client as redis_client_module
import app.database.session as database_session_module

_DATABASE_URL = "postgresql+asyncpg://apexscan:unit-test-password@postgres:5432/apexscan"
_REDIS_URL = "redis://redis:6379/0"


class _FakeEngine:
    """Minimal external SQLAlchemy engine boundary for lifecycle tests."""

    def __init__(self, connection: _FakeConnection | None = None) -> None:
        self.dispose = AsyncMock()
        self.connect = Mock(return_value=connection)


class _FakeConnection:
    """Minimal async database connection that records the dependency probe."""

    def __init__(self) -> None:
        self.execute = AsyncMock()

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeSession:
    """Minimal request-scoped session with observable cleanup operations."""

    def __init__(self) -> None:
        self.rollback = AsyncMock()
        self.close = AsyncMock()


class _FakeRedisPool:
    """Minimal external Redis pool boundary for lifecycle tests."""

    def __init__(self) -> None:
        self.aclose = AsyncMock()


class _FakeRedisClient:
    """Minimal request-scoped Redis client with an observable ping/close path."""

    def __init__(self, connection_pool: _FakeRedisPool) -> None:
        self.connection_pool = connection_pool
        self.ping = AsyncMock(return_value=True)
        self.aclose = AsyncMock()


def _database_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    engine: _FakeEngine,
    session: _FakeSession | None = None,
) -> object:
    """Build a lifecycle around external fakes without opening a real connection."""
    monkeypatch.setattr(
        database_session_module,
        "create_async_engine",
        Mock(return_value=engine),
    )
    if session is not None:
        monkeypatch.setattr(
            database_session_module,
            "async_sessionmaker",
            Mock(return_value=Mock(return_value=session)),
        )

    return database_session_module.DatabaseLifecycle()


async def test_database_disposes_initialized_engine_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated cleanup releases one initialized engine and clears its managed state."""
    engine = _FakeEngine()
    lifecycle = _database_lifecycle(monkeypatch, engine=engine)

    await lifecycle.initialize(_DATABASE_URL)
    assert lifecycle.engine is engine

    await lifecycle.dispose()
    await lifecycle.dispose()

    engine.dispose.assert_awaited_once()
    assert lifecycle.is_initialized is False


async def test_database_session_closes_after_successful_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A managed session closes after normal use without an implicit commit."""
    session = _FakeSession()
    lifecycle = _database_lifecycle(monkeypatch, engine=_FakeEngine(), session=session)

    await lifecycle.initialize(_DATABASE_URL)
    async with lifecycle.session() as acquired:
        assert acquired is session

    session.rollback.assert_not_awaited()
    session.close.assert_awaited_once()


async def test_database_session_rolls_back_and_closes_on_request_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error inside a request-scoped session cannot leak an open transaction."""
    session = _FakeSession()
    lifecycle = _database_lifecycle(monkeypatch, engine=_FakeEngine(), session=session)

    await lifecycle.initialize(_DATABASE_URL)
    with pytest.raises(RuntimeError, match="transaction failed"):
        async with lifecycle.session():
            raise RuntimeError("transaction failed")

    session.rollback.assert_awaited_once()
    session.close.assert_awaited_once()


async def test_database_connectivity_probe_executes_dependency_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The database probe checks PostgreSQL with SELECT 1, not application data."""
    connection = _FakeConnection()
    lifecycle = _database_lifecycle(monkeypatch, engine=_FakeEngine(connection=connection))

    await lifecycle.initialize(_DATABASE_URL)
    await lifecycle.verify_connectivity()

    statement = connection.execute.await_args.args[0]
    assert str(statement) == "SELECT 1"


async def test_database_partial_initialization_is_cleaned_up_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session-factory failure disposes its engine without exposing a URL secret."""
    engine = _FakeEngine()
    secret = "must-not-appear-in-database-error"
    monkeypatch.setattr(database_session_module, "create_async_engine", Mock(return_value=engine))
    monkeypatch.setattr(
        database_session_module,
        "async_sessionmaker",
        Mock(side_effect=RuntimeError(f"failed for {secret}")),
    )
    lifecycle = database_session_module.DatabaseLifecycle()

    with pytest.raises(database_session_module.DatabaseInitializationError) as captured:
        await lifecycle.initialize(_DATABASE_URL)

    engine.dispose.assert_awaited_once()
    assert lifecycle.is_initialized is False
    assert secret not in str(captured.value)


async def test_redis_closes_initialized_pool_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated cleanup releases one Redis pool and clears its managed state."""
    pool = _FakeRedisPool()
    pool_factory = Mock(return_value=pool)
    monkeypatch.setattr(redis_client_module.redis.ConnectionPool, "from_url", pool_factory)
    monkeypatch.setattr(
        redis_client_module.redis,
        "Redis",
        Mock(return_value=_FakeRedisClient(pool)),
    )
    lifecycle = redis_client_module.RedisLifecycle()

    await lifecycle.initialize(_REDIS_URL)
    client = lifecycle.create_client()
    assert client.connection_pool is pool

    await lifecycle.close()
    await lifecycle.close()

    pool.aclose.assert_awaited_once()
    assert lifecycle.is_initialized is False


async def test_redis_connectivity_probe_closes_its_request_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Redis probe pings the dependency and releases its temporary client."""
    pool = _FakeRedisPool()
    client = _FakeRedisClient(pool)
    monkeypatch.setattr(
        redis_client_module.redis.ConnectionPool, "from_url", Mock(return_value=pool)
    )
    monkeypatch.setattr(redis_client_module.redis, "Redis", Mock(return_value=client))
    lifecycle = redis_client_module.RedisLifecycle()

    await lifecycle.initialize(_REDIS_URL)
    await lifecycle.verify_connectivity()

    client.ping.assert_awaited_once()
    client.aclose.assert_awaited_once()


async def test_redis_runtime_probe_failure_is_redacted_and_releases_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Redis ping remains actionable without disclosing connection details."""
    pool = _FakeRedisPool()
    client = _FakeRedisClient(pool)
    secret = "must-not-appear-in-redis-error"
    client.ping.side_effect = redis_client_module.redis.RedisError(f"failed for {secret}")
    monkeypatch.setattr(
        redis_client_module.redis.ConnectionPool, "from_url", Mock(return_value=pool)
    )
    monkeypatch.setattr(redis_client_module.redis, "Redis", Mock(return_value=client))
    lifecycle = redis_client_module.RedisLifecycle()

    await lifecycle.initialize(_REDIS_URL)
    with pytest.raises(redis_client_module.RedisConnectivityError) as captured:
        await lifecycle.verify_connectivity()

    client.aclose.assert_awaited_once()
    assert secret not in str(captured.value)


async def test_redis_partial_initialization_leaves_no_cached_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Redis pool creation failure leaves the lifecycle retryable and empty."""
    secret = "must-not-appear-in-redis-error"
    monkeypatch.setattr(
        redis_client_module.redis.ConnectionPool,
        "from_url",
        Mock(side_effect=ValueError(f"failed for {secret}")),
    )
    lifecycle = redis_client_module.RedisLifecycle()

    with pytest.raises(redis_client_module.RedisInitializationError) as captured:
        await lifecycle.initialize(_REDIS_URL)

    assert lifecycle.is_initialized is False
    assert secret not in str(captured.value)
