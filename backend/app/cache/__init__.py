"""Cache package.

Redis connection infrastructure for caching and real-time messaging.
Exposes the connection pool and the :func:`get_redis` dependency.
"""

from app.cache.redis_client import (
    RedisCleanupError,
    RedisConnectivityError,
    RedisInitializationError,
    RedisLifecycle,
    RedisLifecycleError,
    RedisNotInitializedError,
    get_redis,
    redis_lifecycle,
)

__all__ = [
    "RedisCleanupError",
    "RedisConnectivityError",
    "RedisInitializationError",
    "RedisLifecycle",
    "RedisLifecycleError",
    "RedisNotInitializedError",
    "get_redis",
    "redis_lifecycle",
]
