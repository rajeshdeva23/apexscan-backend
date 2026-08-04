"""Real PostgreSQL and Redis lifecycle tests, enabled only by explicit opt-in."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

import app.cache.redis_client as redis_client_module
import app.database.session as database_session_module

pytestmark = pytest.mark.integration


@pytest.fixture
def integration_urls() -> tuple[str, str]:
    """Return real service URLs only when a Compose-capable runner opts in."""
    if os.getenv("APEXSCAN_RUN_INTEGRATION") != "1":
        pytest.skip("set APEXSCAN_RUN_INTEGRATION=1 to run real dependency lifecycle tests")

    database_url = os.getenv("INTEGRATION_DATABASE_URL") or os.environ["DATABASE_URL"]
    redis_url = os.getenv("INTEGRATION_REDIS_URL") or os.environ["REDIS_URL"]
    return database_url, redis_url


async def test_postgresql_connects_and_closes_managed_session(
    integration_urls: tuple[str, str],
) -> None:
    """A real PostgreSQL connection supports a scoped session and clean disposal."""
    database_url, _ = integration_urls
    lifecycle = database_session_module.DatabaseLifecycle()

    await lifecycle.initialize(database_url)
    await lifecycle.verify_connectivity()
    async with lifecycle.session() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1

    await lifecycle.dispose()
    await lifecycle.dispose()
    assert lifecycle.is_initialized is False


async def test_redis_connects_and_closes_managed_pool(
    integration_urls: tuple[str, str],
) -> None:
    """A real Redis connection responds to a probe and supports repeated cleanup."""
    _, redis_url = integration_urls
    lifecycle = redis_client_module.RedisLifecycle()

    await lifecycle.initialize(redis_url)
    await lifecycle.verify_connectivity()
    await lifecycle.close()
    await lifecycle.close()

    assert lifecycle.is_initialized is False


async def test_postgresql_unavailable_dependency_raises_typed_error(
    integration_urls: tuple[str, str],
) -> None:
    """A real driver reports an unavailable PostgreSQL endpoint through the safe boundary."""
    _database_url, _redis_url = integration_urls
    lifecycle = database_session_module.DatabaseLifecycle()

    await lifecycle.initialize("postgresql+asyncpg://apexscan:unused@127.0.0.1:1/apexscan")
    with pytest.raises(database_session_module.DatabaseConnectivityError):
        await lifecycle.verify_connectivity()

    await lifecycle.dispose()


async def test_redis_unavailable_dependency_raises_typed_error(
    integration_urls: tuple[str, str],
) -> None:
    """A real driver reports an unavailable Redis endpoint through the safe boundary."""
    _database_url, _redis_url = integration_urls
    lifecycle = redis_client_module.RedisLifecycle()

    await lifecycle.initialize("redis://127.0.0.1:1/0?socket_connect_timeout=0.1")
    with pytest.raises(redis_client_module.RedisConnectivityError):
        await lifecycle.verify_connectivity()

    await lifecycle.close()
