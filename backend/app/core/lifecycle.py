"""Application lifecycle state and dependency orchestration primitives."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.core.config import Settings


class DatabaseDependency(Protocol):
    """The PostgreSQL lifecycle capability required by application startup."""

    async def initialize(self, database_url: str, *, echo: bool = False) -> None:
        """Initialize database resources."""

    async def verify_connectivity(self) -> None:
        """Verify database connectivity."""

    async def dispose(self) -> None:
        """Release database resources."""


class RedisDependency(Protocol):
    """The Redis lifecycle capability required by application startup."""

    async def initialize(self, redis_url: str) -> None:
        """Initialize Redis resources."""

    async def verify_connectivity(self) -> None:
        """Verify Redis connectivity."""

    async def close(self) -> None:
        """Release Redis resources."""


class ApplicationStartupError(RuntimeError):
    """A safe error raised when a mandatory startup dependency is unavailable."""


class ApplicationShutdownError(RuntimeError):
    """A safe error raised when ordered dependency cleanup cannot complete."""


class LifecycleState(StrEnum):
    """The application-level startup and shutdown phase."""

    STARTING = "starting"
    STARTED = "started"
    FAILED = "failed"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"


class DependencyState(StrEnum):
    """The most recent non-sensitive state of a mandatory dependency."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """Non-sensitive lifecycle state suitable for an operational health response."""

    status: str
    startup: str | None = None
    dependencies: dict[str, str] | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the snapshot as a JSON-serializable response payload."""
        payload: dict[str, object] = {"status": self.status}
        if self.startup is not None:
            payload["startup"] = self.startup
        if self.dependencies is not None:
            payload["dependencies"] = self.dependencies
        return payload


class ApplicationLifecycle:
    """Coordinate mandatory dependency startup, health, and ordered cleanup."""

    def __init__(self, database: DatabaseDependency, redis: RedisDependency) -> None:
        self._database = database
        self._redis = redis
        self._state = LifecycleState.STARTING
        self._database_state = DependencyState.UNKNOWN
        self._redis_state = DependencyState.UNKNOWN

    async def start(self, settings: Settings) -> None:
        """Start mandatory dependencies in order and gate application readiness.

        Raises:
            ApplicationStartupError: If a mandatory dependency cannot initialize,
                verify, or safely clean up after a failed startup.
        """
        if self._state is LifecycleState.STARTED:
            return
        if self._state is LifecycleState.SHUTTING_DOWN:
            raise ApplicationStartupError(
                "Application startup is blocked while shutdown is in progress"
            )

        self._state = LifecycleState.STARTING
        self._database_state = DependencyState.UNKNOWN
        self._redis_state = DependencyState.UNKNOWN

        try:
            await self._database.initialize(settings.database_url, echo=settings.app_debug)
            await self._database.verify_connectivity()
            self._database_state = DependencyState.HEALTHY

            await self._redis.initialize(settings.redis_url)
            await self._redis.verify_connectivity()
            self._redis_state = DependencyState.HEALTHY
        except Exception as error:
            if self._database_state is not DependencyState.HEALTHY:
                self._database_state = DependencyState.UNHEALTHY
            if self._redis_state is not DependencyState.HEALTHY:
                self._redis_state = DependencyState.UNHEALTHY

            cleanup_error = await self._release_dependencies()
            self._state = LifecycleState.FAILED
            if cleanup_error is not None:
                raise ApplicationStartupError(
                    "Application startup failed and partial dependency cleanup failed"
                ) from cleanup_error
            raise ApplicationStartupError(
                "Application startup is blocked because a mandatory dependency is unavailable"
            ) from error

        self._state = LifecycleState.STARTED

    async def readiness_snapshot(self) -> HealthSnapshot:
        """Probe mandatory dependencies on demand and return truthful readiness state."""
        if self._state is not LifecycleState.STARTED:
            return self._readiness_response()

        await self._probe_database()
        await self._probe_redis()
        return self._readiness_response()

    def liveness_snapshot(self) -> HealthSnapshot:
        """Return process liveness without probing transient dependencies."""
        return HealthSnapshot(status="live")

    def startup_snapshot(self) -> HealthSnapshot:
        """Return the non-sensitive application startup phase."""
        return HealthSnapshot(status=self._state.value)

    async def shutdown(self) -> None:
        """Stop readiness first, then close Redis before PostgreSQL idempotently.

        Raises:
            ApplicationShutdownError: If dependency cleanup reports an error.
        """
        if self._state in {LifecycleState.SHUTTING_DOWN, LifecycleState.STOPPED}:
            return

        self._state = LifecycleState.SHUTTING_DOWN
        cleanup_error = await self._release_dependencies()
        self._state = LifecycleState.STOPPED
        if cleanup_error is not None:
            raise ApplicationShutdownError(
                "Application shutdown dependency cleanup failed"
            ) from cleanup_error

    async def _probe_database(self) -> None:
        """Refresh PostgreSQL health without exposing probe diagnostics."""
        try:
            await self._database.verify_connectivity()
        except Exception:
            self._database_state = DependencyState.UNHEALTHY
        else:
            self._database_state = DependencyState.HEALTHY

    async def _probe_redis(self) -> None:
        """Refresh Redis health without exposing probe diagnostics."""
        try:
            await self._redis.verify_connectivity()
        except Exception:
            self._redis_state = DependencyState.UNHEALTHY
        else:
            self._redis_state = DependencyState.HEALTHY

    async def _release_dependencies(self) -> Exception | None:
        cleanup_error: Exception | None = None
        for close_dependency in (self._redis.close, self._database.dispose):
            try:
                await close_dependency()
            except Exception as error:
                if cleanup_error is None:
                    cleanup_error = error
        return cleanup_error

    def _readiness_response(self) -> HealthSnapshot:
        is_ready = (
            self._state is LifecycleState.STARTED
            and self._database_state is DependencyState.HEALTHY
            and self._redis_state is DependencyState.HEALTHY
        )
        return HealthSnapshot(
            status="ready" if is_ready else "not_ready",
            startup=self._state.value,
            dependencies={
                "database": self._database_state.value,
                "redis": self._redis_state.value,
            },
        )
