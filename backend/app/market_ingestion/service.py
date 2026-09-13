"""Market-ingestion service lifecycle (DECOUPLING PHASE H2 / H3A).

H2 gave the service a real provider lifecycle (Dhan auth/provider/WebSocket) with IPC OFF. H3A
adds the **publisher mode**: when ``ipc_publisher_enabled`` the service owns the M1/D1/M2/L1
publication stack and routes decoded events to Redis via a :class:`PublishingEventSink`, while the
backend legacy path stays authoritative and the IPC consumer/C1 stay OFF.

Frozen startup order (ADR-026): Redis-backed ``boundary.start()`` (allocates the **M1 epoch** via
``publisher.start()``) → L1 ``producer_started`` → bounded L1 diagnostics observer → provider
connect → subscribe → RUNNING. Fail-closed: a terminal publication break (overflow / worker fault /
Redis outage) stops provider intake, disconnects the provider, and marks the service FAILED —
never a silent drop and never a reconnect loop. Importing this module is pure: the publication
stack (heavy ``market_ipc`` imports) is injected by the composition root / tests, never imported
at module load.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from app.adapters.base.broker_adapter import BrokerAdapter, LiveMarketDataAdapter
from app.adapters.base.provider_coordinator import ProviderCoordinator
from app.market_ingestion.errors import PublicationTerminalError
from app.market_ingestion.mode import (
    MarketPathMode,
    PhaseHFlags,
    derive_market_path_mode,
    validate_phase_h_flags,
)
from app.market_ingestion.sink import EventSink, ProviderOnlyEventSink
from app.market_ingestion.supervisor import ProviderSupervisor
from app.schemas.market_data import SubscriptionRequest

if TYPE_CHECKING:
    from app.market_ingestion.publication import PublicationStack

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
    """Raised when an enabled service is started without its required composition."""


@dataclass(frozen=True, slots=True)
class ServiceDiagnostics:
    """Bounded, credential-free ingestion-service snapshot (H2 + H3A publisher fields)."""

    enabled: bool
    status: ServiceStatus
    mode: MarketPathMode
    provider_connected: bool
    events_total: int
    reconnect_total: int
    last_failure: str | None
    # Publisher-mode fields (None/0 in provider-only mode). The L1 accepted position is tracked at
    # sequence granularity; the published position is tracked at count granularity (published_total)
    # in H3A — a sequence-level published position would require a new publisher diagnostic that H3A
    # deliberately does not add (it would touch the D1 transmit path).
    producer_epoch: int | None
    last_accepted_sequence: int | None
    published_total: int
    queue_depth: int
    queue_capacity: int
    overflow_total: int
    publication_failure_total: int
    continuity_state: str | None
    continuity_reason: str | None


class MarketIngestionService:
    """Provider lifecycle (H2) + shadow-publish publisher mode (H3A); inert when disabled."""

    def __init__(
        self,
        *,
        flags: PhaseHFlags,
        provider: BrokerAdapter | None = None,
        subscription_request: SubscriptionRequest | None = None,
        publication: PublicationStack | None = None,
        provider_lifecycle_timeout_seconds: float = 30.0,
        supervisor_max_reconnects: int | None = None,
        supervisor_sleep: Callable[[float], Awaitable[None]] | None = None,
        observer_interval_seconds: float = 0.5,
        observer_sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Validate flags and store injected composition; construct no live dependency (no I/O)."""
        validate_phase_h_flags(flags)
        self._flags = flags
        self._provider = provider
        self._request = subscription_request
        self._publication = publication
        self._timeout = provider_lifecycle_timeout_seconds
        self._max_reconnects = supervisor_max_reconnects
        self._supervisor_sleep = supervisor_sleep
        self._observer_interval = observer_interval_seconds
        self._observer_sleep = observer_sleep or asyncio.sleep
        self._sink: EventSink = ProviderOnlyEventSink()  # publishing sink replaces this in pub mode
        self._coordinator: ProviderCoordinator | None = None
        self._supervisor: ProviderSupervisor | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._observer_task: asyncio.Task[None] | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._terminal = asyncio.Event()
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
    def publisher_mode(self) -> bool:
        """Whether IPC publication is enabled for this incarnation (H3A)."""
        return self._flags.ipc_publisher_enabled

    @property
    def mode(self) -> MarketPathMode:
        """Rollout mode derived from the configured flags."""
        return derive_market_path_mode(self._flags)

    @property
    def provider(self) -> BrokerAdapter | None:
        """The injected provider instance (stable across reconnects); None when disabled/absent."""
        return self._provider

    @property
    def terminal_failure(self) -> bool:
        """Whether this incarnation hit a terminal publication break (fail-closed signal).

        Set by a supervisor that ended on a :class:`PublicationTerminalError` or by the observer
        detecting a broken boundary. Stays set through ``stop()`` so a caller/entrypoint can fail
        closed at the process boundary (non-zero exit) instead of reading a drained ``STOPPED`` as
        a clean shutdown. A clean supervisor end (stream returned / reconnect budget) never sets it.
        """
        return self._terminal.is_set()

    async def start(self) -> None:
        """Boot the lifecycle when enabled; no-op when disabled. Fail closed on a startup error."""
        if not self._flags.market_ingestion_service_enabled:
            self._status = ServiceStatus.DISABLED
            logger.info("market-ingestion service disabled; inert (no Dhan/IPC/Redis activity)")
            return
        self._require_composition()
        self._status = ServiceStatus.STARTING
        try:
            if self.publisher_mode:
                await self._start_publication()  # M1 epoch → L1 incarnation → observer
            await self._start_provider()  # connect + health, then the stream supervisor
            self._status = ServiceStatus.RUNNING
        except Exception:
            await self._cleanup_after_failed_start()
            self._status = ServiceStatus.FAILED
            raise

    def _require_composition(self) -> None:
        """Fail fast if the enabled service lacks its required injected composition."""
        if self._provider is None or self._request is None:
            raise MarketIngestionConfigurationError(
                "an enabled market-ingestion service requires a provider and subscription request"
            )
        if self.publisher_mode and self._publication is None:
            raise MarketIngestionConfigurationError(
                "publisher mode requires a publication stack (M1/D1/M2/L1)"
            )

    async def _start_publication(self) -> None:
        """Start M2 (allocates the M1 epoch), open the L1 incarnation, and start the observer."""
        stack = self._publication
        assert stack is not None  # guaranteed by _require_composition
        self._sink = (
            stack.sink
        )  # publishing sink; supervisor consumes it via the EventSink protocol
        await stack.boundary.start()  # publisher.start() allocates the durable M1 epoch
        epoch = stack.publisher.diagnostics().producer_epoch
        if epoch is None:  # boundary.start() must have allocated it; fail closed otherwise
            raise MarketIngestionConfigurationError(
                "M1 epoch was not allocated by boundary.start()"
            )
        stack.continuity.producer_started(producer_id=stack.producer_id, producer_epoch=epoch)
        self._observer_task = asyncio.create_task(self._run_observer())
        self._watch_task = asyncio.create_task(self._watch_terminal())

    async def _start_provider(self) -> None:
        """Connect + health-check the provider, then start the ordered stream supervisor."""
        self._coordinator = ProviderCoordinator(self._provider)
        await self._coordinator.start(self._timeout)
        self._provider_connected = True
        on_disconnect: Callable[[], None] | None = None
        on_reconnect: Callable[[], None] | None = None
        if self.publisher_mode and self._publication is not None:
            continuity = self._publication.continuity
            continuity.provider_connected()
            on_disconnect = continuity.provider_disconnected
            on_reconnect = continuity.provider_connected
        self._supervisor = ProviderSupervisor(
            provider=cast("LiveMarketDataAdapter", self._provider),
            request=cast("SubscriptionRequest", self._request),
            sink=self._sink,
            sleep=self._supervisor_sleep,
            max_reconnects=self._max_reconnects,
            on_disconnect=on_disconnect,
            on_reconnect=on_reconnect,
        )
        self._supervisor_task = asyncio.create_task(self._supervisor.run())
        self._supervisor_task.add_done_callback(self._on_supervisor_done)

    def _on_supervisor_done(self, task: asyncio.Task[None]) -> None:
        """A supervisor that ends with an exception (e.g. terminal break) triggers fail-closed."""
        if task.cancelled():
            return
        if task.exception() is not None:
            self._terminal.set()

    async def _run_observer(self) -> None:
        """Bounded L1 observer: feed M2 diagnostics into continuity; trip terminal on a break."""
        from app.market_ipc.continuity import ContinuityState  # lazy: keep import pure

        stack = self._publication
        assert stack is not None
        while True:
            stack.continuity.observe_boundary(stack.boundary.diagnostics())
            if stack.continuity.state is ContinuityState.BROKEN:
                self._terminal.set()
                return
            await self._observer_sleep(self._observer_interval)

    async def _watch_terminal(self) -> None:
        """Await a terminal break signal, then fail closed (single owner of the disconnect)."""
        await self._terminal.wait()
        await self._fail_closed()

    async def _fail_closed(self) -> None:
        """Stop intake, disconnect the provider, mark FAILED (idempotent; never self-heals)."""
        if self._status in (ServiceStatus.FAILED, ServiceStatus.STOPPING, ServiceStatus.STOPPED):
            return
        self._status = ServiceStatus.FAILED
        self._provider_connected = False
        await self._cancel_task(self._supervisor_task)
        self._supervisor_task = None
        await self._cancel_task(self._observer_task)
        self._observer_task = None
        if self._coordinator is not None:
            with contextlib.suppress(Exception):
                await self._coordinator.shutdown()

    async def _cleanup_after_failed_start(self) -> None:
        """Unwind anything started during a failed start (reverse order; no leaked task/conn)."""
        self._provider_connected = False
        for task in (self._supervisor_task, self._observer_task, self._watch_task):
            await self._cancel_task(task)
        self._supervisor_task = self._observer_task = self._watch_task = None
        if self._coordinator is not None:
            with contextlib.suppress(Exception):
                await self._coordinator.shutdown()
        if self.publisher_mode and self._publication is not None:
            with contextlib.suppress(Exception):
                await self._publication.boundary.stop()

    async def stop(self) -> None:
        """Stop intake, drain M2, disconnect the provider deterministically (idempotent)."""
        if self._status in (ServiceStatus.STOPPED, ServiceStatus.DISABLED):
            return
        self._status = ServiceStatus.STOPPING
        await self._cancel_task(self._supervisor_task)  # stop provider intake first
        self._supervisor_task = None
        if self._coordinator is not None:  # disconnect provider
            with contextlib.suppress(Exception):
                await self._coordinator.shutdown()
        if self.publisher_mode and self._publication is not None:  # M2 bounded drain → L1 final
            result = await self._publication.boundary.stop()
            self._publication.continuity.drain_completed(result)
        await self._cancel_task(self._observer_task)
        await self._cancel_task(self._watch_task)
        self._observer_task = self._watch_task = None
        self._provider_connected = False
        self._status = ServiceStatus.STOPPED

    async def wait(self) -> None:
        """Block until the supervisor task ends; no-op if none.

        A terminal publication break propagates out of the supervisor task; it is handled by the
        terminal watcher (fail-closed), so ``wait`` swallows it here — it must not escape into the
        caller/entrypoint and skip graceful shutdown. Ordinary cancellation is likewise tolerated.
        """
        task = self._supervisor_task
        if task is None:
            return
        with contextlib.suppress(asyncio.CancelledError, PublicationTerminalError):
            await task

    @staticmethod
    async def _cancel_task(task: asyncio.Task[None] | None) -> None:
        """Cancel and await one task, tolerating normal cancellation."""
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def diagnostics(self) -> ServiceDiagnostics:
        """Snapshot the bounded service/provider/publication status (credential-free)."""
        supervisor = self._supervisor
        base = dict(
            enabled=self.enabled,
            status=self._status,
            mode=self.mode,
            provider_connected=self._provider_connected,
            events_total=self._sink_events(),
            reconnect_total=supervisor.reconnect_total if supervisor is not None else 0,
            last_failure=supervisor.last_failure if supervisor is not None else None,
            producer_epoch=None,
            last_accepted_sequence=None,
            published_total=0,
            queue_depth=0,
            queue_capacity=0,
            overflow_total=0,
            publication_failure_total=0,
            continuity_state=None,
            continuity_reason=None,
        )
        if self.publisher_mode and self._publication is not None:
            base.update(self._publication_diagnostics())
        return ServiceDiagnostics(**base)  # type: ignore[arg-type]

    def _sink_events(self) -> int:
        """Total decoded events routed to the current sink (counting or publishing)."""
        diagnostics = getattr(self._sink, "diagnostics", None)
        return diagnostics().events_total if diagnostics is not None else 0

    def _publication_diagnostics(self) -> dict[str, object]:
        """Publisher-mode L1/boundary fields for the diagnostics snapshot."""
        stack = self._publication
        assert stack is not None
        snapshot = stack.continuity.snapshot()
        boundary = stack.boundary.diagnostics()
        return dict(
            producer_epoch=snapshot.producer_epoch,
            last_accepted_sequence=snapshot.last_accepted_sequence,
            published_total=boundary.published_total,
            queue_depth=boundary.queue_depth,
            queue_capacity=boundary.queue_capacity,
            overflow_total=snapshot.overflow_total,
            publication_failure_total=snapshot.publication_failure_total,
            continuity_state=snapshot.state.value,
            continuity_reason=snapshot.reason.value,
        )
