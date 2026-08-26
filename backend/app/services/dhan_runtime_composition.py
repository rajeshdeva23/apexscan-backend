"""Dhan live-market runtime composition boundary (ADR-010 D4/D5; RUN-B).

Provider-specific composition lives here, in the application/service layer. This
module **may** use Dhan-specific types; the broker-neutral
:class:`LiveMarketRuntime` it builds receives only a canonical
``tuple[Instrument, ...]``. No Dhan type (``DhanRestAdapter``,
``DhanCashEquityLiveUniverse``, ``DhanInstrumentReference``, ``security_id``,
``exchange_segment``) crosses into :class:`LiveMarketRuntime`, the Market Engine,
the Strategy Manager, or ``MarketContext``.

RUN-B scope: explicit provider-enabled composition, the provider lifecycle
(``ProviderCoordinator`` connect/health/shutdown), the governed NSE cash-equity
F&O universe (ADR-004), its reduction to canonical instruments, and fail-closed
cleanup. It starts no live stream and owns no long-running task — that is RUN-C.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

import httpx
from pydantic import ValidationError

from app.adapters.base.broker_adapter import (
    BrokerAdapter,
    HistoricalDataAdapter,
    LiveMarketDataAdapter,
    SessionStatisticsSource,
)
from app.adapters.base.provider_coordinator import ProviderCoordinator
from app.adapters.dhan.adapter import DhanRestAdapter
from app.adapters.dhan.calendar_monitor_parser import DhanMarketHolidayParser
from app.adapters.dhan.calendar_monitor_source import DhanMarketHolidaySource
from app.adapters.dhan.models import DhanCashEquityLiveUniverse
from app.core.config import Settings
from app.market_engine.calendar_data import (
    TradingCalendarDataset,
    load_nse_cm_2026_dataset,
)
from app.market_engine.clock import Clock, SystemClock
from app.market_engine.context import MarketState
from app.market_engine.historical.service import HistoricalWarmupService
from app.market_engine.sequence import SequenceGenerator
from app.market_engine.session import MarketSessionClassifier, SessionSchedule
from app.schemas.market_data import (
    FeedContinuityEvent,
    Instrument,
    ProviderHealth,
    ProviderStatus,
)
from app.services.calendar_monitor import CalendarMonitorService
from app.services.cross_instrument_scanner import ScannerSnapshot
from app.services.historical_source_bridge import broker_historical_source
from app.services.market_runtime import (
    LiveMarketRuntime,
    RequirementsFactory,
    RuntimeRequirementContext,
    RuntimeRequirements,
    RuntimeState,
    _parse_time,
    _schedule_and_calendar,
)
from app.services.session_statistics_activation import SessionStatisticsRefreshCoordinator
from app.services.session_statistics_refresh import SessionStatisticsRefreshService
from app.services.strategy_catalog import StrategyCatalog, production_catalog
from app.services.strategy_requirements_wiring import (
    build_historical_warmup_service,
    build_requirements_coordinator,
)

logger = logging.getLogger(__name__)


class UniverseResolutionError(RuntimeError):
    """Raised when an enabled provider yields no usable canonical universe."""


class AuthoritativeCalendarUnavailableError(RuntimeError):
    """Raised when the authoritative packaged calendar dataset cannot be loaded/validated.

    A composition/startup error (ADR-011 calendar-dataset-failure-policy DF2), distinct from
    provider authentication/connectivity failures and :class:`UniverseResolutionError`: in the
    provider-enabled path the authoritative dataset is mandatory, so a missing, mis-encoded,
    malformed, or invalid dataset fails composition fast rather than silently promoting a legacy
    (``settings.nse_holidays``) or secondary (Dhan monitor) calendar to authority. The underlying
    cause is preserved via ``raise ... from``; no dataset contents or provider payload are
    included in the message.
    """


class _DeferredContinuitySink:
    """A feed-continuity sink whose Market-Engine target is bound once the runtime exists.

    Resolves the construction cycle (the adapter is built before the runtime): the sink is
    passed to the adapter at construction and bound to ``TickEngine.on_feed_continuity``
    after the runtime is composed. It fails closed if invoked before binding, which cannot
    happen in normal operation because no live stream runs before ``runtime.start()``.
    """

    def __init__(self) -> None:
        self._target: Callable[[FeedContinuityEvent], None] | None = None

    def bind(self, target: Callable[[FeedContinuityEvent], None]) -> None:
        """Point the sink at the runtime's broker-neutral continuity entry point."""
        self._target = target

    def __call__(self, event: FeedContinuityEvent) -> None:
        """Forward one canonical continuity fact to the bound Market-Engine target."""
        if self._target is None:
            raise RuntimeError("feed-continuity event received before runtime binding")
        self._target(event)


class _DeferredLiveSessionGate:
    """A live-session predicate bound once the runtime's classifier and clock exist.

    Mirrors :class:`_DeferredContinuitySink`: it is passed to the adapter at construction so
    the stale-feed watchdog fires only during an expected ``LIVE_SESSION``, and it is bound
    after the runtime is composed. It fails closed (returns ``False``) while unbound, so an
    unbound gate can never trigger a reconnect.
    """

    def __init__(self) -> None:
        self._predicate: Callable[[], bool] | None = None

    def bind(self, predicate: Callable[[], bool]) -> None:
        """Point the gate at the composed classifier's live-session decision."""
        self._predicate = predicate

    def __call__(self) -> bool:
        """Return whether an expected live regular session is currently in progress."""
        return self._predicate() if self._predicate is not None else False


@runtime_checkable
class LiveUniverseAdapter(Protocol):
    """The provider capabilities RUN-B composition requires.

    Combines the broker-neutral lifecycle + instrument-master contract with the
    Dhan-specific cash-equity universe resolver, which is legal on this side of the
    boundary. Satisfied structurally by :class:`DhanRestAdapter`.
    """

    async def connect(self) -> None:
        """Open provider connections without issuing a market request."""

    async def disconnect(self) -> None:
        """Release provider connections safely."""

    async def get_health(self) -> ProviderHealth:
        """Return a canonical provider-health observation."""

    async def load_instruments(self) -> tuple[Instrument, ...]:
        """Load the provider instrument master as canonical instruments."""

    def load_nse_cash_equity_live_universe(self) -> DhanCashEquityLiveUniverse:
        """Resolve the governed NSE cash-equity live universe (Dhan-specific type)."""


@dataclass(frozen=True, slots=True)
class RuntimeComposition:
    """Owns the composed runtime and its provider lifecycle until RUN-E integration.

    The provider is held as the broker-neutral :class:`ProviderCoordinator`, never
    the concrete adapter. Coordinated shutdown lives here so the provider is never
    leaked while ``ApplicationLifecycle`` integration is still pending (RUN-E).
    """

    runtime: LiveMarketRuntime
    provider_coordinator: ProviderCoordinator | None

    async def start(self) -> None:
        """Start the runtime (subscribe the manager) after the core is fully composed."""
        await self.runtime.start()

    async def shutdown(self) -> None:
        """Shut down the runtime, then the provider lifecycle (idempotent, no leak)."""
        await self.runtime.shutdown()
        if self.provider_coordinator is not None:
            await self.provider_coordinator.shutdown()


def _canonical_cash_equity_universe(live: DhanCashEquityLiveUniverse) -> tuple[Instrument, ...]:
    """Reduce the Dhan-specific cash-equity universe to canonical instruments (fail-closed).

    Preserves the provider's deterministic ``(exchange, symbol)`` ordering, requires a
    populated universe (ADR-004: a bounded, non-empty F&O-eligible cash-equity set), and
    rejects duplicate canonical instruments rather than silently collapsing them.
    """
    instruments = tuple(reference.instrument for reference in live.cash_references)
    if not instruments:
        raise UniverseResolutionError(
            "enabled provider resolved an empty cash-equity universe; ADR-004 expects a "
            "bounded, non-empty F&O-eligible set"
        )
    if len(set(instruments)) != len(instruments):
        raise UniverseResolutionError("duplicate canonical instruments in the resolved universe")
    return instruments


def _historical_warmup(
    *,
    dataset: TradingCalendarDataset,
    provider: LiveUniverseAdapter,
    settings: Settings,
    schedule: SessionSchedule,
    context: RuntimeRequirementContext,
) -> HistoricalWarmupService:
    """Build the historical warmup stack over the dataset's calendar/coverage/overrides.

    The dataset is the single historical calendar authority: its ``trading_calendar()``,
    ``calendar_coverage()``, and per-date session overrides drive the planner, while the
    ``SessionSchedule`` hours and exchange timezone stay settings-governed. It is always a
    validated dataset — the enabled path fails fast (``AuthoritativeCalendarUnavailableError``)
    before composing warmup when the dataset cannot load, so historical and live share one
    authority boundary (ADR-011 calendar-dataset-failure-policy DF5). Pure construction — no
    provider I/O runs until a strategy START drives warmup.
    """
    return build_historical_warmup_service(
        source=broker_historical_source(cast("HistoricalDataAdapter", provider)),
        registry=context.registry,
        schedule=schedule,
        exchange_timezone=settings.exchange_timezone,
        calendar=dataset.trading_calendar(),
        coverage=dataset.calendar_coverage(),
        candles=context.candle_engine,
        overrides=dataset.session_overrides_domain(),
    )


def _dhan_requirements_factory(
    *,
    provider: LiveUniverseAdapter,
    settings: Settings,
    dataset: TradingCalendarDataset,
    clock: Clock | None,
) -> RequirementsFactory:
    """Build a broker-neutral requirements factory over the composed Dhan provider.

    Fact/live requirement seams and the session-statistics refresh coordinator are always
    composed (they need no historical calendar authority). Historical warmup is wired onto the
    DATASET-derived trading calendar, coverage, and session overrides; the ``dataset`` is always
    a validated one because the enabled path fails fast when it cannot load (ADR-011
    calendar-dataset-failure-policy DF5), so there is no ``UnavailableHistoricalWarmup`` fallback
    here. The ``SessionSchedule`` hours and exchange timezone remain settings-governed. The
    factory is pure — no provider I/O runs until a strategy START drives warmup, and it never
    calls ``refresh_if_due``.
    """
    schedule, _ = _schedule_and_calendar(settings)

    def factory(context: RuntimeRequirementContext) -> RuntimeRequirements:
        refresh_service = SessionStatisticsRefreshService(
            source=cast("SessionStatisticsSource", provider),
            registry=context.registry,
            clock=clock or SystemClock(),
        )
        refresh_coordinator = SessionStatisticsRefreshCoordinator(
            service=refresh_service, instruments=context.instruments
        )
        warmup_service = _historical_warmup(
            dataset=dataset,
            provider=provider,
            settings=settings,
            schedule=schedule,
            context=context,
        )
        coordinator = build_requirements_coordinator(
            instruments=context.instruments,
            historical=context.historical_requirements,
            engine=context.candle_engine,
            warmup=warmup_service,
            live=context.live_timeframe_requirements,
            fact_requirements=context.fact_requirements,
            refresh_control=refresh_coordinator,
        )
        return RuntimeRequirements(
            coordinator=coordinator, session_statistics_refresh=refresh_coordinator
        )

    return factory


async def _safe_shutdown(coordinator: ProviderCoordinator) -> None:
    """Best-effort provider cleanup during fail-closed composition unwinding."""
    try:
        await coordinator.shutdown()
    except Exception:
        logger.exception("provider cleanup failed while unwinding runtime composition")


def _live_session_classifier(
    *, settings: Settings, dataset: TradingCalendarDataset
) -> MarketSessionClassifier:
    """Build the coverage-aware live classifier from the resolved dataset (LC5/LC6).

    The validated ``dataset`` is the sole date-level authority: the live classifier consumes
    its ``trading_calendar()`` (closed dates + exceptional OPEN sessions) and its
    ``calendar_coverage()``, so any date outside coverage classifies as ``CALENDAR_UNAVAILABLE``
    at classify time (ADR-011 out-of-coverage addendum LC13). The ``SessionSchedule`` hours and
    exchange timezone stay settings-governed. The same resolved ``dataset`` object is reused — it
    is never loaded twice.

    The enabled path always supplies a validated dataset: composition fails fast
    (``AuthoritativeCalendarUnavailableError``) before reaching here when the dataset cannot
    load, so a live classifier is never built over ``settings.nse_holidays`` in the enabled path
    (ADR-011 calendar-dataset-failure-policy DF4/DF13). The disabled/no-provider runtime, which
    carries no live data, keeps its own settings-derived classifier in ``LiveMarketRuntime`` and
    never reaches this function.
    """
    schedule, _ = _schedule_and_calendar(settings)
    return MarketSessionClassifier(
        schedule=schedule,
        calendar=dataset.trading_calendar(),
        exchange_timezone=settings.exchange_timezone,
        coverage=dataset.calendar_coverage(),
    )


def _calendar_monitor(
    *,
    settings: Settings,
    dataset: TradingCalendarDataset,
    transport: httpx.AsyncBaseTransport | None,
    clock: Clock | None,
) -> CalendarMonitorService | None:
    """Build the secondary calendar monitor when enabled, else ``None`` (ADR-011).

    Off by default: when ``calendar_monitor_enabled`` is false no monitor and no network
    are wired. When enabled, the Dhan source (over the SAME injected ``transport`` the
    composition threads, so tests stay offline) and parser feed a broker-neutral
    :class:`CalendarMonitorService` over the SAME resolved ``dataset`` the runtime uses.
    """
    if not settings.calendar_monitor_enabled:
        return None
    source = DhanMarketHolidaySource(
        transport=transport,
        timeout_seconds=settings.calendar_monitor_request_timeout_seconds,
    )
    return CalendarMonitorService(
        source=source,
        parser=DhanMarketHolidayParser(),
        dataset=dataset,
        clock=clock or SystemClock(),
    )


def _resolve_calendar_dataset(
    dataset: TradingCalendarDataset | None,
) -> TradingCalendarDataset:
    """Resolve the authoritative calendar dataset, failing fast when it cannot load.

    An injected ``dataset`` (tests) is used verbatim. Otherwise the governed packaged NSE 2026
    Capital-Market dataset is loaded and validated. Any load/validation failure is **fail-fast**:
    a missing/unreadable resource (``OSError``), a mis-encoded resource (``UnicodeDecodeError``),
    or malformed/invalid dataset content (``ValidationError`` — bad JSON, inverted coverage,
    OPEN/CLOSED conflict, invalid intervals, missing provenance) raises
    :class:`AuthoritativeCalendarUnavailableError` with the cause preserved. This is the enabled
    path's single authority boundary: the market runtime never starts without calendar authority
    and never falls back to ``settings.nse_holidays`` or the Dhan monitor (ADR-011
    calendar-dataset-failure-policy DF1-DF5). The narrow ``except`` never swallows programmer
    defects (``TypeError``/``AttributeError`` propagate). No dataset contents are logged.
    """
    if dataset is not None:
        return dataset
    try:
        return load_nse_cm_2026_dataset()
    except (ValidationError, OSError, UnicodeDecodeError) as error:
        raise AuthoritativeCalendarUnavailableError(
            "authoritative packaged calendar dataset could not be loaded or validated"
        ) from error


async def compose_market_runtime(
    *,
    settings: Settings,
    error_threshold: int,
    adapter: LiveUniverseAdapter | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    calendar_dataset: TradingCalendarDataset | None = None,
    catalog: StrategyCatalog | None = None,
    clock: Clock | None = None,
    sequence: SequenceGenerator | None = None,
) -> RuntimeComposition:
    """Compose the live-market runtime, resolving the universe only when provider-enabled.

    Disabled mode returns a bare broker-neutral runtime (empty universe, no provider).
    Enabled mode constructs the Dhan adapter at this boundary, starts the provider
    coordinator (connect + healthy probe), resolves the governed cash-equity universe,
    reduces it to canonical instruments, resolves the authoritative calendar dataset, and
    builds the runtime exactly once. Any provider/universe/calendar failure cleans up the
    provider and propagates — no partial runtime, no manager subscription (the runtime is
    returned un-started; call ``start()``).

    Args:
        settings: Governed settings; ``market_provider_enabled`` selects the mode.
        error_threshold: Strategy-Manager consecutive-failure threshold (RUN-A).
        adapter: Injected provider (tests); defaults to ``DhanRestAdapter.from_settings``.
        transport: Optional HTTP transport for the default adapter and the calendar monitor
            (tests inject ``httpx.MockTransport``).
        calendar_dataset: The single date-level calendar authority (calendar, coverage, and
            session overrides) for historical warmup, the live classifier, and the monitor. When
            ``None`` the packaged NSE 2026 dataset is loaded; a load/validation failure is
            **fail-fast** (``AuthoritativeCalendarUnavailableError``) — the enabled runtime never
            starts without calendar authority and never falls back to ``settings.nse_holidays``
            or the Dhan monitor (ADR-011 calendar-dataset-failure-policy). When resolved, the same
            dataset object builds the coverage-aware live classifier (out-of-coverage dates
            ``CALENDAR_UNAVAILABLE``) and, when enabled, the secondary monitor.
        catalog: The production strategy catalog (ADR-013); defaults to
            :func:`~app.services.strategy_catalog.production_catalog`. Only the entries named
            by ``settings.strategies_enabled`` are registered/started; an unknown enabled id
            fails closed (``UnknownEnabledStrategyError``, provider coordinator cleaned up).
        clock: Injected clock; production uses the system clock.
        sequence: Injected sequence generator.

    Raises:
        UniverseResolutionError: If the enabled universe is empty or has duplicates.
        AuthoritativeCalendarUnavailableError: If the authoritative calendar dataset cannot be
            loaded or validated (provider coordinator cleaned up first).
        UnknownEnabledStrategyError: If an enabled strategy id is absent from the catalog.
        ProviderLifecycleError: If provider start/health fails (from the coordinator).
    """
    if not settings.market_provider_enabled:
        runtime = LiveMarketRuntime(
            settings=settings, error_threshold=error_threshold, clock=clock, sequence=sequence
        )
        return RuntimeComposition(runtime=runtime, provider_coordinator=None)

    sink: _DeferredContinuitySink | None = None
    session_gate: _DeferredLiveSessionGate | None = None
    if adapter is not None:
        provider: LiveUniverseAdapter = adapter
    else:
        sink = _DeferredContinuitySink()
        session_gate = _DeferredLiveSessionGate()
        provider = DhanRestAdapter.from_settings(
            settings,
            transport=transport,
            live_continuity_sink=sink,
            live_session_predicate=session_gate,
        )
    coordinator = ProviderCoordinator(cast("BrokerAdapter", provider))
    try:
        await coordinator.start(settings.provider_lifecycle_timeout_seconds)
        await provider.load_instruments()
        universe = _canonical_cash_equity_universe(provider.load_nse_cash_equity_live_universe())
        dataset = _resolve_calendar_dataset(calendar_dataset)
        entries = (catalog or production_catalog()).resolve(settings.strategies_enabled_list)
    except BaseException:
        await _safe_shutdown(coordinator)
        raise
    requirements_factory = _dhan_requirements_factory(
        provider=provider, settings=settings, dataset=dataset, clock=clock
    )
    monitor = _calendar_monitor(
        settings=settings, dataset=dataset, transport=transport, clock=clock
    )
    session_classifier = _live_session_classifier(settings=settings, dataset=dataset)
    runtime = LiveMarketRuntime(
        settings=settings,
        error_threshold=error_threshold,
        instruments=universe,
        live_market_data=cast("LiveMarketDataAdapter", provider),
        requirements_factory=requirements_factory,
        session_statistics_refresh_poll_seconds=settings.session_statistics_refresh_poll_seconds,
        calendar_monitor=monitor,
        calendar_monitor_run_time=(
            _parse_time(settings.calendar_monitor_run_time) if monitor is not None else None
        ),
        strategies=tuple(entry.strategy for entry in entries),
        configurations={entry.strategy_id: entry.configuration for entry in entries},
        scanner_ranking_policies=tuple(
            entry.ranking_policy for entry in entries if entry.ranking_policy is not None
        ),
        autostart_strategy_ids=tuple(entry.strategy_id for entry in entries),
        clock=clock,
        sequence=sequence,
        session_classifier=session_classifier,
    )
    if sink is not None:
        sink.bind(runtime.tick_engine.on_feed_continuity)  # same TickEngine the runtime owns
    if session_gate is not None:
        session_clock = clock or SystemClock()

        def _in_live_session() -> bool:
            state = session_classifier.classify(session_clock.now()).market_state
            return state is MarketState.LIVE_SESSION

        session_gate.bind(_in_live_session)
    return RuntimeComposition(runtime=runtime, provider_coordinator=coordinator)


class LiveMarketRuntimeDependency:
    """A ``ProviderDependency`` that lazily composes and owns the live-market runtime.

    ``ApplicationLifecycle`` owns exactly this one broker-neutral dependency (ADR-010
    D1/D2). Construction performs **no** I/O; all provider/universe/ingestion work happens
    in :meth:`start` (inside the application lifespan). :meth:`verify_health` combines the
    provider coordinator's health with the live-ingestion state, and :meth:`shutdown`
    delegates to :class:`RuntimeComposition` (ingestion → manager → provider). Dhan types
    never reach ``app.core.lifecycle``.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        error_threshold: int,
        calendar_dataset: TradingCalendarDataset | None = None,
        adapter: LiveUniverseAdapter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
        sequence: SequenceGenerator | None = None,
    ) -> None:
        """Store composition inputs without performing any I/O."""
        self._settings = settings
        self._error_threshold = error_threshold
        self._calendar_dataset = calendar_dataset
        self._adapter = adapter
        self._transport = transport
        self._clock = clock or SystemClock()
        self._sequence = sequence
        self._composition: RuntimeComposition | None = None

    async def start(self, timeout_seconds: float) -> None:
        """Compose and start the runtime, idempotently.

        ``timeout_seconds`` is part of the ``ProviderDependency`` contract but unused here:
        provider-lifecycle timeouts are applied inside the composition from settings.
        """
        if self._composition is not None:
            return
        composition = await compose_market_runtime(
            settings=self._settings,
            error_threshold=self._error_threshold,
            adapter=self._adapter,
            transport=self._transport,
            calendar_dataset=self._calendar_dataset,
            clock=self._clock,
            sequence=self._sequence,
        )
        try:
            await composition.start()
        except BaseException:
            await composition.shutdown()
            raise
        self._composition = composition

    async def verify_health(self) -> ProviderHealth:
        """Report HEALTHY only when the provider is healthy and ingestion is alive."""
        composition = self._composition
        if composition is None:
            return ProviderHealth(status=ProviderStatus.UNKNOWN, observed_at=self._clock.now())
        provider_healthy = True
        if composition.provider_coordinator is not None:
            provider_health = await composition.provider_coordinator.verify_health()
            provider_healthy = provider_health.status is ProviderStatus.HEALTHY
        status = composition.runtime.status()
        healthy = (
            provider_healthy
            and status.ingestion_running
            and not status.fatal_ingestion_error
            and not status.fatal_refresh_driver_error
        )
        return ProviderHealth(
            status=ProviderStatus.HEALTHY if healthy else ProviderStatus.DOWN,
            observed_at=self._clock.now(),
        )

    async def shutdown(self) -> None:
        """Shut the runtime down (ingestion → manager → provider); safe if never started."""
        composition = self._composition
        self._composition = None
        if composition is not None:
            await composition.shutdown()

    # ----------------------------------------------------------------------- #
    # ScannerSnapshotSource read seam (ADR-012 API15) — no construction, no I/O
    # ----------------------------------------------------------------------- #
    def scanner_read_available(self) -> bool:
        """Return whether a scanner read is safe: composed, STARTED, and no fatal task."""
        composition = self._composition
        if composition is None:
            return False
        status = composition.runtime.status()
        return status.state is RuntimeState.STARTED and not (
            status.fatal_ingestion_error
            or status.fatal_refresh_driver_error
            or status.fatal_calendar_monitor_error
        )

    def scannable_strategy_ids(self) -> tuple[str, ...]:
        """Return the scanner-enabled strategy ids, or empty when not composed."""
        composition = self._composition
        return composition.runtime.scannable_strategy_ids() if composition is not None else ()

    def scanner_snapshot(self, strategy_id: str) -> ScannerSnapshot | None:
        """Return the current ranked snapshot for ``strategy_id``, or ``None`` when absent."""
        composition = self._composition
        if composition is None:
            return None
        return composition.runtime.scanner_snapshot(strategy_id)
