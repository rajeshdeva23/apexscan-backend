"""Broker-neutral live-market runtime composition (ADR-010; RUN-A skeleton).

:class:`LiveMarketRuntime` owns the assembled live-market core — one shared
:class:`EventBus`, :class:`InstrumentStateRegistry`, :class:`CandleEngine`,
:class:`TickEngine`, and :class:`StrategyManager` — and the runtime lifecycle
(subscribe the manager on start, unsubscribe on shutdown). It is
composition/service-layer infrastructure: it is **not** the Market Engine, a
provider, or the Strategy Manager, and it imports **no** concrete provider
adapter (ADR-010 D1/D4).

RUN-A is the skeleton slice. It composes only objects that can be constructed
truthfully without provider I/O, credentials, or a resolved instrument universe:
it creates no ingestion task, performs no network I/O, and starts no provider.
Session-statistics authority is disabled for both canonical sources (ADR-009 /
E6A); enabling it is a later evidence-gated slice. Provider construction, universe
resolution, live ingestion, and the refresh driver are RUN-B/RUN-C/RUN-D concerns
that attach to the read-only composition seams exposed here.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from types import MappingProxyType

from app.adapters.base.broker_adapter import LiveMarketDataAdapter
from app.core.config import Settings
from app.events.bus import EventBus
from app.market_engine.candle_engine import CandleEngine
from app.market_engine.clock import Clock, SystemClock
from app.market_engine.historical.requirements import HistoricalRequirementRegistry
from app.market_engine.sequence import MonotonicSequence, SequenceGenerator
from app.market_engine.session import MarketSessionClassifier, SessionSchedule, TradingCalendar
from app.market_engine.session_statistics import SessionStatisticsAuthority
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.tick_engine import TickEngine
from app.schemas.market_data import (
    Instrument,
    MarketData,
    MarketDataKind,
    ProviderHealth,
    ProviderStatus,
    Quote,
    SubscriptionRequest,
    Tick,
)
from app.services.calendar_monitor import CalendarMonitorDriver, CalendarMonitorRun
from app.services.cross_instrument_scanner import (
    CrossInstrumentStrategyScanner,
    ScannerRankingPolicy,
    ScannerRankingPolicyRegistry,
    ScannerSnapshot,
)
from app.services.session_statistics_driver import (
    DrivenSessionStatisticsRefresh,
    SessionStatisticsRefreshDriver,
)
from app.strategies.configuration import StrategyConfiguration
from app.strategies.contracts import Strategy
from app.strategies.registry import StrategyRegistry
from app.strategy_manager.fact_requirements import FactRequirementRegistry
from app.strategy_manager.lifecycle import StrategyLifecycle
from app.strategy_manager.live_timeframes import LiveTimeframeRequirementRegistry
from app.strategy_manager.manager import StrategyManager
from app.strategy_manager.requirements_bridge import RequirementsCoordinator

logger = logging.getLogger(__name__)

_TIME_FORMAT = "%H:%M"
# The scanner subscribes trade ticks only: this yields a canonical Tick-only stream
# (the provider filters to the requested kinds), which is exactly what TickEngine.process
# consumes — no unprocessable depth/candle frames reach the ingestion loop.
_LIVE_DATA_TYPES = frozenset({MarketDataKind.TICK})
_NO_CONFIGURATIONS: Mapping[str, StrategyConfiguration] = MappingProxyType({})


class UnsupportedLiveDatumError(RuntimeError):
    """Raised when the live stream yields a datum the TickEngine cannot process."""


@dataclass(frozen=True, slots=True)
class RuntimeRequirementContext:
    """The shared Market-Engine objects a :class:`RequirementsCoordinator` is built over.

    Handed to an injected requirements factory so the coordinator (and its historical
    warmup + refresh-control seams) is composed over the *same* registry, candle engine,
    universe, and requirement registries the runtime owns — never duplicates (ADR-010 D3).
    """

    instruments: tuple[Instrument, ...]
    registry: InstrumentStateRegistry
    candle_engine: CandleEngine
    historical_requirements: HistoricalRequirementRegistry
    live_timeframe_requirements: LiveTimeframeRequirementRegistry
    fact_requirements: FactRequirementRegistry


@dataclass(frozen=True, slots=True)
class RuntimeRequirements:
    """The composed requirement wiring the runtime installs (ADR-010 D3; RUN-D/E7).

    Attributes:
        coordinator: The requirements coordinator the Strategy Manager drives.
        session_statistics_refresh: The driven refresh capability (E5 coordinator) the
            managed refresh driver invokes, or ``None`` when no session-statistics source
            is composed.
    """

    coordinator: RequirementsCoordinator
    session_statistics_refresh: DrivenSessionStatisticsRefresh | None


# A composition-supplied builder that wires the coordinator (with provider-derived
# historical/session-statistics seams) over the runtime's shared objects. Broker-neutral.
RequirementsFactory = Callable[[RuntimeRequirementContext], RuntimeRequirements]


class RuntimeState(StrEnum):
    """The live-market runtime's lifecycle phase.

    ``NOT_STARTED`` → ``STARTED`` → ``SHUTDOWN``. Shutdown is terminal: RUN-A does
    not support restart (ADR-010 D11; matches the application lifecycle convention).
    """

    NOT_STARTED = "not_started"
    STARTED = "started"
    SHUTDOWN = "shutdown"


class RuntimeLifecycleError(RuntimeError):
    """Raised on an invalid live-market runtime lifecycle transition."""


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """An immutable, non-sensitive snapshot of the runtime's composed state.

    Attributes:
        state: The current lifecycle phase.
        manager_subscribed: Whether the Strategy Manager is subscribed to the bus.
        known_instrument_count: Size of the composed instrument universe.
        active_timeframe_count: Active CandleEngine timeframes (0 until demand arrives).
        staged_observation_verified: The staged-observation authority bit (always False).
        tick_aggregate_verified: The tick-aggregate authority bit (always False).
        ingestion_configured: Whether a live-market-data source is wired.
        ingestion_running: Whether the single ingestion task is currently active.
        fatal_ingestion_error: Whether ingestion ended unexpectedly (failed/exhausted).
        refresh_driver_configured: Whether the session-statistics refresh driver is wired.
        refresh_driver_running: Whether the refresh-driver task is currently active.
        fatal_refresh_driver_error: Whether the refresh driver ended unexpectedly.
        calendar_monitor_configured: Whether the secondary calendar monitor is wired.
        calendar_monitor_running: Whether the calendar-monitor task is currently active.
        fatal_calendar_monitor_error: Whether the calendar monitor ended unexpectedly.
    """

    state: RuntimeState
    manager_subscribed: bool
    known_instrument_count: int
    active_timeframe_count: int
    staged_observation_verified: bool
    tick_aggregate_verified: bool
    ingestion_configured: bool
    ingestion_running: bool
    fatal_ingestion_error: bool
    refresh_driver_configured: bool
    refresh_driver_running: bool
    fatal_refresh_driver_error: bool
    calendar_monitor_configured: bool
    calendar_monitor_running: bool
    fatal_calendar_monitor_error: bool


def _parse_time(value: str) -> time:
    """Parse a validated exchange-local ``HH:MM`` time string (settings are pre-validated)."""
    return datetime.strptime(value.strip(), _TIME_FORMAT).time()


def _schedule_and_calendar(settings: Settings) -> tuple[SessionSchedule, TradingCalendar]:
    """Build the governed NSE session schedule and trading calendar from settings.

    Provider-blind: reads only the governed ``nse_*``/timezone configuration
    (ADR-004; docs/06 §8). No hard-coded session times live in the runtime.
    """
    schedule = SessionSchedule(
        pre_open_start=_parse_time(settings.nse_pre_open_start),
        opening_auction_start=_parse_time(settings.nse_opening_auction_start),
        regular_open=_parse_time(settings.nse_regular_open),
        regular_close=_parse_time(settings.nse_regular_close),
        closing_end=_parse_time(settings.nse_closing_end),
    )
    holidays = [
        date.fromisoformat(entry.strip())
        for entry in settings.nse_holidays.split(",")
        if entry.strip()
    ]
    return schedule, TradingCalendar(holidays=holidays)


class LiveMarketRuntime:
    """Owns the assembled live-market core and its lifecycle (ADR-010; RUN-A skeleton).

    Implements the shape of the application ``ProviderDependency`` lifecycle
    (:meth:`start`, :meth:`verify_health`, :meth:`shutdown`) so the top-level
    ``ApplicationLifecycle`` can own it through its existing provider seam (wired in
    a later slice). In RUN-A there is no provider, so :meth:`verify_health` reports
    ``UNKNOWN`` — it never claims a provider is connected.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        error_threshold: int,
        instruments: Sequence[Instrument] = (),
        live_market_data: LiveMarketDataAdapter | None = None,
        requirements_factory: RequirementsFactory | None = None,
        strategies: Sequence[Strategy] = (),
        configurations: Mapping[str, StrategyConfiguration] = _NO_CONFIGURATIONS,
        session_statistics_refresh_poll_seconds: float = 1.0,
        calendar_monitor: CalendarMonitorRun | None = None,
        calendar_monitor_run_time: time | None = None,
        calendar_monitor_poll_seconds: float = 60.0,
        scanner_ranking_policies: Sequence[ScannerRankingPolicy] = (),
        autostart_strategy_ids: Sequence[str] = (),
        clock: Clock | None = None,
        sequence: SequenceGenerator | None = None,
        session_classifier: MarketSessionClassifier | None = None,
    ) -> None:
        """Compose the shared runtime core (no provider I/O in the constructor).

        Args:
            settings: Governed application settings (session schedule, timezone).
            error_threshold: Consecutive-failure threshold for the Strategy Manager;
                required with no default (docs/07 §20 leaves the number open).
            instruments: The canonical instrument universe; the same tuple seeds the
                registry and the live SubscriptionRequest (ADR-010 D3/D6).
            live_market_data: The broker-neutral live-data source. When ``None`` the
                runtime is dormant — :meth:`start` runs no ingestion (disabled mode).
            requirements_factory: A composition-supplied builder that assembles the
                :class:`RequirementsCoordinator` over the runtime's shared objects
                (ADR-010 D3). When ``None`` the Strategy Manager routes only (no
                lifecycle commands); pure construction — the factory performs no I/O.
            strategies: Concrete strategies to register; empty in production (ADR-010 D15).
            configurations: Per-strategy configuration keyed by strategy id; empty in
                production.
            session_statistics_refresh_poll_seconds: The refresh driver's wake interval
                (infrastructure, not the refresh cadence — ADR-009 addendum).
            calendar_monitor: The neutral secondary-calendar-monitor capability (ADR-011);
                ``None`` (default) wires no monitor task. The Dhan source/parser reach the
                runtime only through this neutral capability — the runtime stays Dhan-free.
            calendar_monitor_run_time: The exchange-local time at/after which the monitor's
                once-per-day check fires; required alongside ``calendar_monitor``.
            calendar_monitor_poll_seconds: The monitor driver's wake interval (infrastructure,
                not the run cadence).
            scanner_ranking_policies: Composition-owned cross-instrument scanner ranking
                policies (ADR-012). The runtime always composes a passive
                :class:`CrossInstrumentStrategyScanner` over these; an empty tuple (default)
                leaves the scanner enabled but ranking nothing (results for strategies without
                a policy are ignored). It adds no task and issues no historical/provider calls.
            autostart_strategy_ids: The registered strategy ids to START during :meth:`start`
                (ADR-013 REG3/REG6). Empty (default) preserves REGISTERED ≠ RUNNING — passing
                ``strategies`` registers them without starting. Each id is started via
                ``manager.start`` with per-strategy failure isolation; no new task is created.
            clock: Injected clock; production uses :class:`SystemClock`.
            sequence: Injected sequence generator; defaults to a fresh monotonic one.
            session_classifier: The governed session classifier. When ``None`` (default)
                one is built from settings with no calendar coverage (legacy behaviour for
                the disabled/no-live path). Provider-enabled composition injects a
                coverage-aware classifier built from the resolved dataset's calendar and
                coverage so out-of-coverage dates classify as ``CALENDAR_UNAVAILABLE``
                (ADR-011 live out-of-coverage addendum LC5/LC6). The ``CandleEngine`` always
                keeps the settings ``SessionSchedule`` regardless (LC17).

        Raises:
            ValueError: If ``error_threshold`` is not positive (from the manager).
        """
        known = tuple(instruments)
        self._instruments = known
        self._live_market_data = live_market_data
        self._ingestion_task: asyncio.Task[None] | None = None
        self._ingestion_error: BaseException | None = None
        self._ingestion_failed = False
        self._refresh_poll_seconds = session_statistics_refresh_poll_seconds
        self._refresh_driver_task: asyncio.Task[None] | None = None
        self._refresh_driver_failed = False
        self._exchange_timezone = settings.exchange_timezone
        self._calendar_monitor = calendar_monitor
        self._calendar_monitor_run_time = calendar_monitor_run_time
        self._calendar_monitor_poll_seconds = calendar_monitor_poll_seconds
        self._calendar_monitor_task: asyncio.Task[None] | None = None
        self._calendar_monitor_failed = False
        self._autostart_strategy_ids = tuple(autostart_strategy_ids)
        self._clock: Clock = clock or SystemClock()
        self._sequence: SequenceGenerator = sequence or MonotonicSequence()
        self._bus = EventBus()
        self._scanner = CrossInstrumentStrategyScanner(
            instruments=known,
            policies=ScannerRankingPolicyRegistry(scanner_ranking_policies),
            bus=self._bus,
        )
        self._registry = InstrumentStateRegistry(known)
        self._known_count = len(known)
        schedule, calendar = _schedule_and_calendar(settings)
        self._session = session_classifier or MarketSessionClassifier(
            schedule=schedule, calendar=calendar, exchange_timezone=settings.exchange_timezone
        )
        self._candles = CandleEngine(
            schedule=schedule, exchange_timezone=settings.exchange_timezone, timeframes=()
        )
        # Both canonical session-statistics sources stay unverified in production
        # (ADR-009 / E6A / E6B): a valid aggregate never becomes AUTHORITATIVE here.
        self._authority = SessionStatisticsAuthority()
        self._tick_engine = TickEngine(
            registry=self._registry,
            bus=self._bus,
            clock=self._clock,
            sequence=self._sequence,
            session=self._session,
            candles=self._candles,
            session_statistics_authority=self._authority,
        )
        self._strategy_registry = StrategyRegistry()
        self._strategy_lifecycle = StrategyLifecycle()
        for strategy in strategies:  # empty in production (zero concrete strategies)
            self._strategy_registry.register(strategy)
            self._strategy_lifecycle.register(strategy.descriptor.strategy_id)
        self._historical_requirements = HistoricalRequirementRegistry()
        self._live_timeframe_requirements = LiveTimeframeRequirementRegistry()
        self._fact_requirements = FactRequirementRegistry()
        # Build the coordinator over the runtime's shared objects, then construct the
        # manager once with it (no post-construction rebind). None ⇒ routing-only.
        requirements = self._build_requirements(requirements_factory)
        self._requirements_coordinator = requirements.coordinator if requirements else None
        self._session_statistics_refresh = (
            requirements.session_statistics_refresh if requirements else None
        )
        self._manager = StrategyManager(
            registry=self._strategy_registry,
            lifecycle=self._strategy_lifecycle,
            configurations=configurations,
            error_threshold=error_threshold,
            bus=self._bus,
            requirements=self._requirements_coordinator,
        )
        self._state = RuntimeState.NOT_STARTED
        self._manager_subscribed = False

    def _build_requirements(
        self, factory: RequirementsFactory | None
    ) -> RuntimeRequirements | None:
        """Invoke the injected factory over the runtime's shared objects (pure, no I/O)."""
        if factory is None:
            return None
        context = RuntimeRequirementContext(
            instruments=self._instruments,
            registry=self._registry,
            candle_engine=self._candles,
            historical_requirements=self._historical_requirements,
            live_timeframe_requirements=self._live_timeframe_requirements,
            fact_requirements=self._fact_requirements,
        )
        return factory(context)

    # ----------------------------------------------------------------------- #
    # Lifecycle (ProviderDependency-shaped)
    # ----------------------------------------------------------------------- #
    async def start(self, timeout_seconds: float = 0.0) -> None:
        """Subscribe the Strategy Manager and mark the runtime started (idempotent).

        Subscribing before any (future RUN-C) ingestion begins guarantees no initial
        ``MarketContext`` is lost. ``timeout_seconds`` is part of the provider-lifecycle
        contract and is unused in the skeleton (no provider is started in RUN-A).

        Raises:
            RuntimeLifecycleError: If called after :meth:`shutdown` (shutdown is terminal).
        """
        if self._state is RuntimeState.SHUTDOWN:
            raise RuntimeLifecycleError("live-market runtime cannot restart after shutdown")
        if self._state is RuntimeState.STARTED:
            return
        self._manager.subscribe()  # subscribers installed before the first live datum
        self._scanner.subscribe()  # aggregates StrategyResultsPublished; installed pre-ingestion
        self._manager_subscribed = True
        self._state = RuntimeState.STARTED
        for strategy_id in self._autostart_strategy_ids:
            await self._start_strategy(strategy_id)  # warm requirements before ingestion (D10)
        if self._live_market_data is not None and self._instruments:
            self._ingestion_task = asyncio.create_task(self._run_ingestion())
            self._ingestion_task.add_done_callback(self._on_ingestion_done)
        if self._session_statistics_refresh is not None and self._session is not None:
            driver = SessionStatisticsRefreshDriver(
                refresh=self._session_statistics_refresh,
                classifier=self._session,
                clock=self._clock,
                poll_seconds=self._refresh_poll_seconds,
            )
            self._refresh_driver_task = asyncio.create_task(driver.run())
            self._refresh_driver_task.add_done_callback(self._on_refresh_driver_done)
        if self._calendar_monitor is not None and self._calendar_monitor_run_time is not None:
            monitor_driver = CalendarMonitorDriver(
                monitor=self._calendar_monitor,
                clock=self._clock,
                exchange_timezone=self._exchange_timezone,
                run_time=self._calendar_monitor_run_time,
                poll_seconds=self._calendar_monitor_poll_seconds,
            )
            self._calendar_monitor_task = asyncio.create_task(monitor_driver.run())
            self._calendar_monitor_task.add_done_callback(self._on_calendar_monitor_done)

    async def _start_strategy(self, strategy_id: str) -> None:
        """Start one enabled strategy, isolating any failure to that strategy (ADR-013 REG6).

        ``manager.start`` already resolves a config/warmup fault to ``ERROR`` (retaining
        requirements); this also guards an unexpected raise (e.g. a missing coordinator) so a
        single strategy never aborts runtime start or another strategy. No task is created.
        """
        try:
            await self._manager.start(strategy_id, reference=self._clock.now())
        except Exception:
            logger.exception("enabled strategy %s failed to start; skipped", strategy_id)

    async def _run_ingestion(self) -> None:
        """Consume the live stream sequentially into the TickEngine (the one owned task).

        One ``async for`` over the provider stream dispatches each datum synchronously
        before requesting the next — preserving per-instrument ordering, the monotonic
        sequence, and one-datum-one-version semantics (ADR-010 D8/D10). Reconnect is the
        adapter's responsibility (ADR-006); this loop adds none.
        """
        assert self._live_market_data is not None
        request = SubscriptionRequest(instruments=self._instruments, data_types=_LIVE_DATA_TYPES)
        async for datum in self._live_market_data.stream_market_data(request):
            self._dispatch(datum)

    def _dispatch(self, datum: MarketData) -> None:
        """Route one canonical datum to the TickEngine, fail-closed on an unsupported type."""
        if isinstance(datum, (Tick, Quote)):
            self._tick_engine.process(datum)
            return
        raise UnsupportedLiveDatumError(
            f"live stream yielded an unprocessable datum: {type(datum).__name__}"
        )

    def _on_ingestion_done(self, task: asyncio.Task[None]) -> None:
        """Observe ingestion-task completion so a failure/exhaustion is never lost.

        Cancellation is the normal shutdown path and is not a fault. Any other terminal
        state — an exception, or a normal return from the never-completing stream — is a
        fatal ingestion condition (the stream is a ``while True`` live feed).
        """
        if task.cancelled():
            return
        self._ingestion_error = task.exception()
        self._ingestion_failed = True
        logger.error(
            "live ingestion task ended unexpectedly (%s)",
            "exhausted" if self._ingestion_error is None else type(self._ingestion_error).__name__,
        )

    def _on_refresh_driver_done(self, task: asyncio.Task[None]) -> None:
        """Observe refresh-driver completion. Cancellation is normal; any other end is fatal.

        Ordinary per-cycle provider failures are swallowed inside the driver (E4/E5 fail
        closed), so reaching here without cancellation means an unexpected fatal error.
        """
        if task.cancelled():
            return
        self._refresh_driver_failed = True
        error = task.exception()
        logger.error(
            "session-statistics refresh driver ended unexpectedly (%s)",
            "returned" if error is None else type(error).__name__,
        )

    def _on_calendar_monitor_done(self, task: asyncio.Task[None]) -> None:
        """Observe calendar-monitor completion. Cancellation is normal; any other end is fatal.

        The monitor swallows its own per-cycle fetch/parse failures, so reaching here without
        cancellation means an unexpected fatal error in the driver loop.
        """
        if task.cancelled():
            return
        self._calendar_monitor_failed = True
        error = task.exception()
        logger.error(
            "calendar monitor driver ended unexpectedly (%s)",
            "returned" if error is None else type(error).__name__,
        )

    async def _cancel_task(self, task: asyncio.Task[None] | None) -> None:
        """Cancel and await one owned task, suppressing the expected cancellation."""
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def verify_health(self) -> ProviderHealth:
        """Return a truthful provider-health observation.

        In the RUN-A skeleton no provider is wired, so health is ``UNKNOWN`` — the
        runtime never claims a provider is connected. Later slices report the
        provider coordinator's real health here.
        """
        return ProviderHealth(status=ProviderStatus.UNKNOWN, observed_at=self._clock.now())

    async def shutdown(self) -> None:
        """Stop ingestion, then unsubscribe the Strategy Manager (idempotent, no leak).

        Order (ADR-010 D11): mark shut down first (no new work); cancel and **await** the
        ingestion task, then the refresh driver, then unsubscribe the manager. The outer
        composition disconnects the provider afterwards, so no provider work races an
        in-flight task. Safe before start or twice.
        """
        if self._state is RuntimeState.SHUTDOWN:
            return
        self._state = RuntimeState.SHUTDOWN
        await self._cancel_task(self._ingestion_task)  # 1. stop ingestion
        await self._cancel_task(self._refresh_driver_task)  # 2. stop refresh driver
        await self._cancel_task(self._calendar_monitor_task)  # 3. stop calendar monitor
        self._scanner.unsubscribe()  # 4. detach the scanner aggregator
        self._manager.unsubscribe()  # 5. unsubscribe the manager
        self._manager_subscribed = False

    def status(self) -> RuntimeStatus:
        """Return an immutable, non-sensitive snapshot of the composed runtime state."""
        ingestion = self._ingestion_task
        driver = self._refresh_driver_task
        monitor = self._calendar_monitor_task
        return RuntimeStatus(
            state=self._state,
            manager_subscribed=self._manager_subscribed,
            known_instrument_count=self._known_count,
            active_timeframe_count=len(self._candles.timeframes),
            staged_observation_verified=self._authority.staged_observation_verified,
            tick_aggregate_verified=self._authority.tick_aggregate_verified,
            ingestion_configured=self._live_market_data is not None,
            ingestion_running=ingestion is not None and not ingestion.done(),
            fatal_ingestion_error=self._ingestion_failed,
            refresh_driver_configured=self._session_statistics_refresh is not None,
            refresh_driver_running=driver is not None and not driver.done(),
            fatal_refresh_driver_error=self._refresh_driver_failed,
            calendar_monitor_configured=self._calendar_monitor is not None,
            calendar_monitor_running=monitor is not None and not monitor.done(),
            fatal_calendar_monitor_error=self._calendar_monitor_failed,
        )

    # ----------------------------------------------------------------------- #
    # Read-only composition seams (for RUN-B/C/D wiring and invariant tests)
    # ----------------------------------------------------------------------- #
    @property
    def scanner(self) -> CrossInstrumentStrategyScanner:
        """The cross-instrument scanner aggregator (subscribed on start; ADR-012)."""
        return self._scanner

    def scanner_snapshot(self, strategy_id: str) -> ScannerSnapshot | None:
        """Return the current ranked scanner snapshot for ``strategy_id``, or ``None``."""
        return self._scanner.snapshot(strategy_id)

    def scannable_strategy_ids(self) -> tuple[str, ...]:
        """Return the scanner-enabled strategy ids (ADR-012 API17)."""
        return self._scanner.scannable_strategy_ids()

    @property
    def state(self) -> RuntimeState:
        """The current lifecycle phase."""
        return self._state

    @property
    def manager_subscribed(self) -> bool:
        """Whether the Strategy Manager is currently subscribed to the shared bus."""
        return self._manager_subscribed

    @property
    def bus(self) -> EventBus:
        """The single shared in-process event bus (publisher: engine; subscriber: manager)."""
        return self._bus

    @property
    def registry(self) -> InstrumentStateRegistry:
        """The single shared per-instrument state registry (ADR-010 D3)."""
        return self._registry

    @property
    def candle_engine(self) -> CandleEngine:
        """The shared candle engine (RUN-D drives its timeframe set via the sink)."""
        return self._candles

    @property
    def session_classifier(self) -> MarketSessionClassifier:
        """The governed session classifier (used by the engine and the future driver)."""
        return self._session

    @property
    def tick_engine(self) -> TickEngine:
        """The tick engine (RUN-C dispatches the live stream into it)."""
        return self._tick_engine

    @property
    def strategy_manager(self) -> StrategyManager:
        """The strategy manager (subscribes to the shared bus on start)."""
        return self._manager

    @property
    def requirements_coordinator(self) -> RequirementsCoordinator | None:
        """The composed requirements coordinator, or None when the manager routes only."""
        return self._requirements_coordinator

    @property
    def session_statistics_refresh(self) -> DrivenSessionStatisticsRefresh | None:
        """The driven session-statistics refresh capability the driver invokes, if composed."""
        return self._session_statistics_refresh

    @property
    def authority(self) -> SessionStatisticsAuthority:
        """The per-source session-statistics authority (both bits disabled)."""
        return self._authority

    @property
    def historical_requirements(self) -> HistoricalRequirementRegistry:
        """The manager-owned historical requirement registry (empty in RUN-A)."""
        return self._historical_requirements

    @property
    def live_timeframe_requirements(self) -> LiveTimeframeRequirementRegistry:
        """The manager-owned live-timeframe requirement registry (empty in RUN-A)."""
        return self._live_timeframe_requirements

    @property
    def fact_requirements(self) -> FactRequirementRegistry:
        """The manager-owned fact requirement registry (no SESSION_STATISTICS demand)."""
        return self._fact_requirements
