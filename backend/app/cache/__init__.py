"""Cache package.

Redis connection infrastructure for caching and real-time messaging.
Exposes the connection pool and the :func:`get_redis` dependency.
"""

from app.cache.redis_client import get_redis, redis_pool

__all__ = ["get_redis", "redis_pool"]
