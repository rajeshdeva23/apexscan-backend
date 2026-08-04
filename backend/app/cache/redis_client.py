"""Redis client management.

Provides a single async Redis connection pool for the process and a
dependency that hands out clients to request handlers. Redis backs caching
and the real-time fan-out layer. No caching strategy or keyspace design is
implemented here in Phase 1 — this is connection infrastructure only.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import redis.asyncio as redis

from app.core.config import get_settings


def create_redis_pool() -> redis.ConnectionPool:
    """Create an async Redis connection pool from application settings."""
    settings = get_settings()
    return redis.ConnectionPool.from_url(
        settings.redis_url,
        decode_responses=True,
        max_connections=20,
    )


# Single connection pool per process.
redis_pool: redis.ConnectionPool = create_redis_pool()


async def get_redis() -> AsyncGenerator[redis.Redis]:
    """Yield a Redis client bound to the shared connection pool.

    FastAPI dependency. The client returns its connection to the pool on exit.
    """
    client: redis.Redis = redis.Redis(connection_pool=redis_pool)
    try:
        yield client
    finally:
        await client.aclose()
