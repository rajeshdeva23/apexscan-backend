"""Market-ingestion service lifecycle (DECOUPLING PHASE H2).

Turns the H1 inert skeleton into a real, independently-bootable provider lifecycle: when enabled it
owns one Dhan auth manager / provider / WebSocket (via an injected provider) coordinated by the
broker-neutral :class:`ProviderCoordinator`, and consumes the live subscription through a
:class:`ProviderSupervisor` into a non-authoritative counting sink. IPC publication stays OFF —
nothing here allocates an M1 epoch, starts M2, constructs the D1 publisher, or touches Redis, and
the ingestion service never imports the backend TickEngine as authority.

Disabled (the default) the service is fully inert (H1 behaviour). Importing this module is pure:
the provider and its dependencies are injected/constructed lazily by the composition root, never at
import time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from app.adapters.base.broker_adapter import BrokerAdapter, LiveMarketDataAdapter
from app.adapters.base.provider_coordinator import ProviderCoordinator
from app.market_ingestion.mode import (
    MarketPathMode,
    PhaseHFlags,
    derive_market_path_mode,
    validate_phase_h_flags,
)
from app.market_ingestion.sink import ProviderOnlyEventSink
from app.market_ingestion.supervisor import ProviderSupervisor
from app.schemas.market_data import SubscriptionRequest

logger = logging.getLogger(__name__)


class ServiceStatus(StrEnum):
    """Ingestion-service lifecycle status (READY only once the provider is connected)."""

    DISABLED = "disabled"
    NOT_STARTED = "not_started"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


class MarketIngestionConfigurationError(RuntimeError):
    """Raised when an enabled service is started without its required provider composition."""


@dataclass(frozen=True, slots=True)
class ServiceDiagnostics:
    """Bounded, credential-free ingestion-service snapshot."""

    enabled: bool
    status: ServiceStatus
    mode: MarketPathMode
    provider_connected: bool
    events_total: int
    reconnect_total: int
    last_failure: str | None


class MarketIngestionService:
    """Provider-lifecycle service — inert when disabled, provider-only boot when enabled (H2).

    IPC publication is not part of this service (H2): no M1 epoch, M2 boundary, D1 publisher, Redis,
    or TickEngine authority is constructed or invoked.
    """

    def __init__(
        self,
        *,
        flags: PhaseHFlags,
        provider: BrokerAdapter | None = None,
        subscription_request: SubscriptionRequest | None = None,
        sink: ProviderOnlyEventSink | None = None,
        provider_lifecycle_timeout_seconds: float = 30.0,
        supervisor_max_reconnects: int | None = None,
        supervisor_sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Validate flags and store injected composition; construct no live dependency (no I/O)."""
        validate_phase_h_flags(flags)
        self._flags = flags
        self._provider = provider
        self._request = subscription_request
        self._sink = sink or ProviderOnlyEventSink()
        self._timeout = provider_lifecycle_timeout_seconds
        self._max_reconnects = supervisor_max_reconnects
        self._supervisor_sleep = supervisor_sleep
        self._coordinator: ProviderCoordinator | None = None
        self._supervisor: ProviderSupervisor | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._provider_connected = False
        self._status = (
            ServiceStatus.NOT_STARTED
            if flags.market_ingestion_service_enabled
            else ServiceStatus.DISABLED
        )

    @property
    def status(self) -> ServiceStatus:
        """Current lifecycle status."""
        return self._status

    @property
    def enabled(self) -> bool:
        """Whether the ingestion service is enabled."""
        return self._flags.market_ingestion_service_enabled

    @property
    def mode(self) -> MarketPathMode:
        """Rollout mode derived from the configured flags."""
        return derive_market_path_mode(self._flags)

    @property
    def provider(self) -> BrokerAdapter | None:
        """The injected provider instance (stable across reconnects); None when disabled/absent."""
        return self._provider

    async def start(self) -> None:
        """Boot the provider lifecycle when enabled; a no-op when disabled (inert).

        Connects and health-checks the provider via the coordinator, then starts the stream
        supervisor. On any startup failure the service cleans up, records FAILED, and re-raises —
        it never reports RUNNING/READY on a partial start. Performs no IPC/M1/M2/D1/Redis work.

        Raises:
            MarketIngestionConfigurationError: If enabled without a provider + subscription request.
        """
        if not self._flags.market_ingestion_service_enabled:
            self._status = ServiceStatus.DISABLED
            logger.info("market-ingestion service disabled; inert (no Dhan/IPC/Redis activity)")
            return
        if self._provider is None or self._request is None:
            raise MarketIngestionConfigurationError(
                "an enabled market-ingestion service requires a provider and subscription request"
            )
        self._status = ServiceStatus.STARTING
        try:
            self._coordinator = ProviderCoordinator(self._provider)
            await self._coordinator.start(self._timeout)  # connect + healthy probe
            self._provider_connected = True
            self._supervisor = ProviderSupervisor(
                provider=cast("LiveMarketDataAdapter", self._provider),
                request=self._request,
                sink=self._sink,
                sleep=self._supervisor_sleep,
                max_reconnects=self._max_reconnects,
            )
            self._supervisor_task = asyncio.create_task(self._supervisor.run())
            self._status = ServiceStatus.RUNNING
        except Exception:
            await self._cleanup_after_failed_start()
            self._status = ServiceStatus.FAILED
            raise

    async def _cleanup_after_failed_start(self) -> None:
        """Release anything constructed during a failed start (no leaked task/connection)."""
        self._provider_connected = False
        await self._cancel_supervisor_task()
        if self._coordinator is not None:
            with contextlib.suppress(Exception):
                await self._coordinator.shutdown()

    async def stop(self) -> None:
        """Stop the supervisor and disconnect the provider deterministically (idempotent)."""
        if self._status in (ServiceStatus.STOPPED, ServiceStatus.DISABLED):
            self._status = (
                ServiceStatus.DISABLED
                if not self._flags.market_ingestion_service_enabled
                else ServiceStatus.STOPPED
            )
            return
        self._status = ServiceStatus.STOPPING
        await self._cancel_supervisor_task()
        if self._coordinator is not None:
            with contextlib.suppress(Exception):
                await self._coordinator.shutdown()
        self._provider_connected = False
        self._status = ServiceStatus.STOPPED

    async def wait(self) -> None:
        """Block until the supervisor task ends (cancelled or reconnect budget exhausted).

        Returns immediately when disabled or not running. Used by the long-running entrypoint to
        keep the process alive for the service's lifetime; tolerates normal cancellation.
        """
        task = self._supervisor_task
        if task is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _cancel_supervisor_task(self) -> None:
        """Cancel and await the supervisor task, tolerating normal cancellation."""
        task = self._supervisor_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._supervisor_task = None

    def diagnostics(self) -> ServiceDiagnostics:
        """Snapshot the bounded service/provider status (credential-free)."""
        supervisor = self._supervisor
        return ServiceDiagnostics(
            enabled=self.enabled,
            status=self._status,
            mode=self.mode,
            provider_connected=self._provider_connected,
            events_total=self._sink.diagnostics().events_total,
            reconnect_total=supervisor.reconnect_total if supervisor is not None else 0,
            last_failure=supervisor.last_failure if supervisor is not None else None,
        )
