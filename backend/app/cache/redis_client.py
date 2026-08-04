"""Managed Redis pool and request-scoped client infrastructure."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import redis.asyncio as redis


class RedisLifecycleError(RuntimeError):
    """Base error for safe Redis lifecycle failures."""


class RedisNotInitializedError(RedisLifecycleError):
    """Raised when Redis resources are requested before initialization."""


class RedisInitializationError(RedisLifecycleError):
    """Raised when Redis pool construction cannot complete safely."""


class RedisConnectivityError(RedisLifecycleError):
    """Raised when Redis cannot be reached by the dependency probe."""


class RedisCleanupError(RedisLifecycleError):
    """Raised when Redis resources cannot be cleanly closed."""


class RedisLifecycle:
    """Own one Redis connection pool and create scoped clients for an app process."""

    def __init__(self) -> None:
        self._pool: redis.ConnectionPool | None = None

    @property
    def is_initialized(self) -> bool:
        """Return whether a managed Redis connection pool is available."""
        return self._pool is not None

    @property
    def _connection_pool(self) -> redis.ConnectionPool:
        if self._pool is None:
            raise RedisNotInitializedError(
                "Redis has not been initialized; initialize the Redis lifecycle first"
            )
        return self._pool

    async def initialize(self, redis_url: str) -> None:
        """Create the managed Redis pool without opening an application client.

        Args:
            redis_url: Validated Redis connection URL.

        Raises:
            RedisInitializationError: If the pool cannot be constructed.
        """
        if self._pool is not None:
            return

        try:
            pool = redis.ConnectionPool.from_url(
                redis_url,
                decode_responses=True,
                max_connections=20,
            )
        except Exception as error:
            raise RedisInitializationError(
                "Redis initialization failed; verify the Redis configuration"
            ) from error

        self._pool = pool

    def create_client(self) -> redis.Redis:
        """Create a request-scoped Redis client bound to the managed pool."""
        return redis.Redis(connection_pool=self._connection_pool)

    async def verify_connectivity(self) -> None:
        """Verify Redis using a temporary client and a dependency-only ping.

        Raises:
            RedisNotInitializedError: If called before initialization.
            RedisConnectivityError: If Redis cannot complete the probe.
        """
        client = self.create_client()
        try:
            if not await client.ping():
                raise RedisConnectivityError(
                    "Redis connectivity verification failed; Redis did not acknowledge ping"
                )
        except RedisLifecycleError:
            raise
        except Exception as error:
            raise RedisConnectivityError(
                "Redis connectivity verification failed; verify service availability"
            ) from error
        finally:
            await client.aclose()

    async def close(self) -> None:
        """Close Redis resources safely and idempotently.

        The pool reference is cleared before closing so a failed close cannot leave
        stale state cached for a later lifecycle retry.
        """
        pool = self._pool
        self._pool = None
        if pool is None:
            return

        try:
            await pool.aclose()
        except Exception as error:
            raise RedisCleanupError(
                "Redis cleanup failed; verify the Redis connection state"
            ) from error


redis_lifecycle = RedisLifecycle()


async def get_redis() -> AsyncGenerator[redis.Redis]:
    """Yield a managed request-scoped Redis client for dependency injection."""
    client = redis_lifecycle.create_client()
    try:
        yield client
    finally:
        await client.aclose()
