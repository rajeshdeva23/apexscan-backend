"""Broker-neutral lifecycle coordination for one required provider adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.adapters.base.broker_adapter import BrokerAdapter
from app.schemas.market_data import ProviderHealth, ProviderStatus

_ProviderResult = TypeVar("_ProviderResult")


class ProviderLifecycleError(RuntimeError):
    """Base error for safe provider lifecycle failures."""


class ProviderConfigurationError(ProviderLifecycleError):
    """Raised when application composition has no required provider adapter."""


class ProviderInitializationError(ProviderLifecycleError):
    """Raised when a required provider cannot be connected and verified."""


class ProviderHealthCheckError(ProviderLifecycleError):
    """Raised when a provider health probe cannot return canonical health."""


class ProviderOperationTimeoutError(ProviderLifecycleError):
    """Raised when a provider lifecycle operation exceeds its bounded timeout."""


class ProviderCleanupError(ProviderLifecycleError):
    """Raised when provider cleanup cannot finish safely."""


class ProviderCoordinator:
    """Own lifecycle coordination for one injected broker-neutral adapter.

    This coordinator performs no broker I/O itself. It bounds lifecycle calls,
    translates adapter exceptions into safe categories, and interprets canonical
    provider health for application readiness.
    """

    def __init__(self, adapter: BrokerAdapter | None = None) -> None:
        self._adapter = adapter
        self._timeout_seconds: float | None = None
        self._connect_attempted = False
        self._started = False

    async def start(self, timeout_seconds: float) -> None:
        """Connect the active adapter once and require a healthy canonical probe."""
        if self._started:
            return
        if self._adapter is None:
            raise ProviderConfigurationError("A required provider adapter is not configured")

        self._timeout_seconds = timeout_seconds
        self._connect_attempted = True
        try:
            await self._run_with_timeout(self._adapter.connect)
        except ProviderOperationTimeoutError:
            raise
        except Exception as error:
            raise ProviderInitializationError(
                "Required provider initialization failed; verify provider availability"
            ) from error

        try:
            health = await self.verify_health()
        except ProviderOperationTimeoutError:
            raise
        except ProviderLifecycleError as error:
            raise ProviderInitializationError(
                "Required provider initialization failed; verify provider availability"
            ) from error

        if health.status is not ProviderStatus.HEALTHY:
            raise ProviderInitializationError("Required provider is not healthy")
        self._started = True

    async def verify_health(self) -> ProviderHealth:
        """Return canonical provider health through the configured timeout boundary."""
        if self._adapter is None:
            raise ProviderHealthCheckError("Required provider health is unavailable")

        try:
            return await self._run_with_timeout(self._adapter.get_health)
        except ProviderOperationTimeoutError:
            raise
        except Exception as error:
            raise ProviderHealthCheckError(
                "Required provider health check failed; verify provider availability"
            ) from error

    async def shutdown(self) -> None:
        """Disconnect a previously attempted provider lifecycle exactly once."""
        adapter = self._adapter
        should_disconnect = self._connect_attempted
        self._connect_attempted = False
        self._started = False
        if adapter is None or not should_disconnect:
            return

        try:
            await self._run_with_timeout(adapter.disconnect)
        except ProviderOperationTimeoutError as error:
            raise ProviderCleanupError("Required provider cleanup timed out") from error
        except Exception as error:
            raise ProviderCleanupError(
                "Required provider cleanup failed; verify provider availability"
            ) from error

    async def _run_with_timeout(
        self, operation: Callable[[], Awaitable[_ProviderResult]]
    ) -> _ProviderResult:
        """Await one lifecycle operation under the configured broker-neutral timeout."""
        timeout_seconds = self._timeout_seconds
        if timeout_seconds is None:
            raise ProviderLifecycleError("Provider lifecycle timeout has not been initialized")

        try:
            async with asyncio.timeout(timeout_seconds):
                return await operation()
        except TimeoutError as error:
            raise ProviderOperationTimeoutError("Provider lifecycle operation timed out") from error
