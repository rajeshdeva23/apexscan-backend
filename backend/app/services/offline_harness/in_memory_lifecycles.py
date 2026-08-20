"""In-memory dependency lifecycles for the offline validation harness.

Satisfy the :class:`DatabaseDependency` / :class:`RedisDependency` protocols with
no external server: every operation is a verified no-op that reports healthy. They
exist solely so the offline harness app can complete startup without PostgreSQL or
Redis; they are never wired into the production composition root (``app.main``).
"""

from __future__ import annotations


class InMemoryDatabaseLifecycle:
    """A :class:`DatabaseDependency` that verifies healthy without a real database."""

    async def initialize(self, database_url: str, *, echo: bool = False) -> None:
        """Accept the database URL without opening a connection."""

    async def verify_connectivity(self) -> None:
        """Report connectivity as healthy (no server is contacted)."""

    async def dispose(self) -> None:
        """Release resources — there are none to release."""


class InMemoryRedisLifecycle:
    """A :class:`RedisDependency` that verifies healthy without a real Redis."""

    async def initialize(self, redis_url: str) -> None:
        """Accept the Redis URL without opening a connection."""

    async def verify_connectivity(self) -> None:
        """Report connectivity as healthy (no server is contacted)."""

    async def close(self) -> None:
        """Release resources — there are none to release."""
