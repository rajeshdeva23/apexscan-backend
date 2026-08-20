"""Strategy requirement-bridge composition & lifecycle (RUN-D; ADR-007/009/010).

Exercises the requirement lifecycle end-to-end through the runtime's Strategy Manager and
its shared RequirementsCoordinator: historical/live/fact registration, effective unions,
retention (PAUSE/RESUME/ERROR) and release (STOP/FORCE STOP), activation-vs-execution, and
shared-instance integration. Uses broker-neutral fake sources — no network, no credentials,
authority disabled throughout.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.core.config import Settings
from app.market_engine.clock import ManualClock
from app.market_engine.historical.calendar_window import CalendarCoverage
from app.market_engine.historical.requirements import HistoricalRequirement
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import (
    Instrument,
    MarketData,
    ProviderSessionOhlc,
    SessionStatisticsObservation,
    SubscriptionRequest,
    Tick,
)
from app.services.historical_source_bridge import DHAN_DIRECT_TIMEFRAMES
from app.services.market_runtime import (
    LiveMarketRuntime,
    RuntimeRequirementContext,
    RuntimeRequirements,
    _schedule_and_calendar,
)
from app.services.session_statistics_activation import SessionStatisticsRefreshCoordinator
from app.services.session_statistics_refresh import SessionStatisticsRefreshService
from app.services.strategy_requirements_wiring import (
    build_historical_warmup_service,
    build_requirements_coordinator,
)
from app.strategies.configuration import StrategyConfiguration
from app.strategies.descriptor import StrategyDescriptor
from app.strategies.enums import (
    CandleCompleteness,
    EmissionPolicy,
    FactNeed,
    StrategyCategory,
    StrategyLifecycleState,
    StrategyTrigger,
)
from app.strategies.requirements import FactFreshnessRequirement, StrategyRequirements
from tests.fakes.historical_source import Behavior, FakeHistoricalSource

_DB = "postgresql+asyncpg://user:pass@localhost:5432/apexscan"
_REDIS = "redis://localhost:6379/0"
_COVERAGE = CalendarCoverage(start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))
_REF = datetime(2026, 8, 6, 4, 0, tzinfo=UTC)  # 09:30 IST on a trading day
_CLOCK = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)
_5M = Timeframe.minutes(5)
_15M = Timeframe.minutes(15)
_5S = timedelta(seconds=5)
_3S = timedelta(seconds=3)
_10S = timedelta(seconds=10)


def _settings() -> Settings:
    return Settings(app_env="development", database_url=_DB, redis_url=_REDIS)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


class _FakeSessionStatsSource:
    """A session-statistics source stub that records how many times it is queried."""

    def __init__(self) -> None:
        self.call_count = 0

    async def load_session_statistics(
        self, instruments: Sequence[Instrument], *, trading_date: date, observed_at: datetime
    ) -> tuple[SessionStatisticsObservation, ...]:
        self.call_count += 1
        return ()


class _RecordingRefreshControl:
    """Wraps the real refresh coordinator to record configure(max_age) calls."""

    def __init__(self, delegate: SessionStatisticsRefreshCoordinator) -> None:
        self._delegate = delegate
        self.configured: list[timedelta | None] = []

    def configure(self, *, max_age: timedelta | None) -> None:
        self.configured.append(max_age)
        self._delegate.configure(max_age=max_age)


class _FakeStrategy:
    """A minimal broker-neutral strategy declaring requirements (never routed here)."""

    def __init__(self, strategy_id: str, requirements: StrategyRequirements) -> None:
        self._id = strategy_id
        self._requirements = requirements

    @property
    def descriptor(self) -> StrategyDescriptor:
        return StrategyDescriptor(
            strategy_id=self._id,
            display_name="Fake",
            description="A test strategy.",
            version="1.0.0",
            category=StrategyCategory.OPENING_SESSION,
            emission_policy=EmissionPolicy.EDGE_TRIGGERED,
        )

    @property
    def requirements(self) -> StrategyRequirements:
        return self._requirements

    @property
    def configuration_type(self) -> type[StrategyConfiguration]:
        return StrategyConfiguration

    def evaluate(self, context: object, configuration: object, metadata: object) -> object:
        raise AssertionError("strategies are not routed in requirement-lifecycle tests")


def _requirements(
    *,
    historical: tuple[HistoricalRequirement, ...] = (),
    live_timeframes: tuple[Timeframe, ...] = (),
    session_statistics_max_age: timedelta | None = None,
) -> StrategyRequirements:
    has_fact = session_statistics_max_age is not None
    return StrategyRequirements(
        trigger=StrategyTrigger.ON_TICK,
        candle_completeness=CandleCompleteness.PARTIAL_ALLOWED,
        historical=historical,
        live_timeframes=live_timeframes,
        fact_needs=(FactNeed.SESSION_STATISTICS,) if has_fact else (),
        freshness=(
            (
                FactFreshnessRequirement(
                    fact=FactNeed.SESSION_STATISTICS, max_age=session_statistics_max_age
                ),
            )
            if has_fact
            else ()
        ),
    )


class _Composed:
    """Bundles a runtime with the fake sources and recording control it was built over."""

    def __init__(
        self,
        runtime: LiveMarketRuntime,
        historical: FakeHistoricalSource,
        session_statistics: _FakeSessionStatsSource,
        control: list[_RecordingRefreshControl],
    ) -> None:
        self.runtime = runtime
        self.historical = historical
        self.session_statistics = session_statistics
        self._control = control

    @property
    def control(self) -> _RecordingRefreshControl:
        return self._control[0]


def _compose(
    strategies: Sequence[_FakeStrategy],
    *,
    historical_behavior: Behavior = Behavior.NORMAL,
    live_market_data: object | None = None,
    session_source: _FakeSessionStatsSource | None = None,
) -> _Composed:
    settings = _settings()
    schedule, calendar = _schedule_and_calendar(settings)
    historical_source = FakeHistoricalSource(
        direct_timeframes=DHAN_DIRECT_TIMEFRAMES, default=historical_behavior
    )
    session_source = session_source if session_source is not None else _FakeSessionStatsSource()
    control_holder: list[_RecordingRefreshControl] = []

    def factory(context: RuntimeRequirementContext) -> RuntimeRequirements:
        refresh_service = SessionStatisticsRefreshService(
            source=session_source, registry=context.registry, clock=ManualClock(_CLOCK)
        )
        refresh_coordinator = SessionStatisticsRefreshCoordinator(
            service=refresh_service, instruments=context.instruments
        )
        control = _RecordingRefreshControl(refresh_coordinator)
        control_holder.append(control)
        warmup = build_historical_warmup_service(
            source=historical_source,
            registry=context.registry,
            schedule=schedule,
            exchange_timezone=settings.exchange_timezone,
            calendar=calendar,
            coverage=_COVERAGE,
            candles=context.candle_engine,
        )
        coordinator = build_requirements_coordinator(
            instruments=context.instruments,
            historical=context.historical_requirements,
            engine=context.candle_engine,
            warmup=warmup,
            live=context.live_timeframe_requirements,
            fact_requirements=context.fact_requirements,
            refresh_control=control,
        )
        return RuntimeRequirements(
            coordinator=coordinator, session_statistics_refresh=refresh_coordinator
        )

    configurations = {
        strategy.descriptor.strategy_id: StrategyConfiguration(config_version="1.0.0")
        for strategy in strategies
    }
    runtime = LiveMarketRuntime(
        settings=settings,
        error_threshold=3,
        instruments=(_instrument(),),
        live_market_data=live_market_data,  # type: ignore[arg-type]
        requirements_factory=factory,  # type: ignore[arg-type]
        strategies=strategies,
        configurations=configurations,
        clock=ManualClock(_CLOCK),
        sequence=MonotonicSequence(),
    )
    return _Composed(runtime, historical_source, session_source, control_holder)


def _historical_union(runtime: LiveMarketRuntime) -> set[tuple[Timeframe, int]]:
    return {
        (r.timeframe, r.lookback) for r in runtime.historical_requirements.effective_requirements()
    }


# --------------------------------------------------------------------------- #
# Composition & shared instances
# --------------------------------------------------------------------------- #
def test_runtime_manager_has_a_real_coordinator() -> None:
    composed = _compose(())
    assert composed.runtime.requirements_coordinator is not None


def test_zero_strategy_startup_provisions_nothing() -> None:
    composed = _compose(())
    assert _historical_union(composed.runtime) == set()
    assert composed.runtime.candle_engine.timeframes == ()
    assert composed.runtime.fact_requirements.is_active() is False
    assert composed.historical.call_count == 0
    assert composed.session_statistics.call_count == 0


# --------------------------------------------------------------------------- #
# Historical requirements
# --------------------------------------------------------------------------- #
async def test_historical_start_registers_and_warms_to_running() -> None:
    strategy = _FakeStrategy(
        "alpha", _requirements(historical=(HistoricalRequirement(timeframe=_5M, lookback=3),))
    )
    composed = _compose((strategy,))
    state = await composed.runtime.strategy_manager.start("alpha", reference=_REF)
    assert state is StrategyLifecycleState.RUNNING
    assert composed.historical.call_count > 0  # warmup used the shared source


async def test_local_source_failure_still_reaches_running_and_retains_requirements() -> None:
    # A per-instrument source failure (HistoricalSourceError) is caught inside warmup and
    # leaves that instrument unsatisfied. Under ADR-007 partial-universe readiness the
    # strategy still reaches RUNNING — the unsatisfied instrument is skipped per-context at
    # evaluation time (no fabrication). The requirement stays registered.
    strategy = _FakeStrategy(
        "alpha", _requirements(historical=(HistoricalRequirement(timeframe=_5M, lookback=3),))
    )
    composed = _compose((strategy,), historical_behavior=Behavior.FAIL)
    state = await composed.runtime.strategy_manager.start("alpha", reference=_REF)
    assert state is StrategyLifecycleState.RUNNING
    assert composed.historical.call_count > 0  # the warmup mechanism actually executed
    assert _historical_union(composed.runtime) == {(_5M, 3)}  # retained (ADR-007 D7)


async def test_global_calendar_failure_prevents_running_but_retains_requirements() -> None:
    # A reference outside authoritative coverage makes warmup raise a GLOBAL failure
    # (OutsideCalendarCoverageError) during planning. START fails closed to ERROR, and the
    # acquired requirement is retained (ADR-007 D7).
    strategy = _FakeStrategy(
        "alpha", _requirements(historical=(HistoricalRequirement(timeframe=_5M, lookback=3),))
    )
    composed = _compose((strategy,))
    out_of_coverage = datetime(2027, 3, 2, 4, 0, tzinfo=UTC)  # beyond _COVERAGE (ends 2026-12-31)
    state = await composed.runtime.strategy_manager.start("alpha", reference=out_of_coverage)
    assert state is StrategyLifecycleState.ERROR
    assert _historical_union(composed.runtime) == {(_5M, 3)}  # retained


async def test_historical_union_max_lookback_and_stop_shrink() -> None:
    a = _FakeStrategy(
        "a", _requirements(historical=(HistoricalRequirement(timeframe=_5M, lookback=100),))
    )
    b = _FakeStrategy(
        "b",
        _requirements(
            historical=(
                HistoricalRequirement(timeframe=_5M, lookback=20),
                HistoricalRequirement(timeframe=_15M, lookback=50),
            )
        ),
    )
    composed = _compose((a, b))
    await composed.runtime.strategy_manager.start("a", reference=_REF)
    await composed.runtime.strategy_manager.start("b", reference=_REF)
    assert _historical_union(composed.runtime) == {(_5M, 100), (_15M, 50)}
    composed.runtime.strategy_manager.stop("a")
    assert _historical_union(composed.runtime) == {(_5M, 20), (_15M, 50)}
    composed.runtime.strategy_manager.stop("b")
    assert _historical_union(composed.runtime) == set()


# --------------------------------------------------------------------------- #
# Live timeframe requirements → shared CandleEngine
# --------------------------------------------------------------------------- #
async def test_live_timeframe_activates_runtime_candle_engine() -> None:
    strategy = _FakeStrategy("alpha", _requirements(live_timeframes=(_5M,)))
    composed = _compose((strategy,))
    await composed.runtime.strategy_manager.start("alpha", reference=_REF)
    assert set(composed.runtime.candle_engine.timeframes) == {_5M}


async def test_live_timeframe_shared_union_and_shrink() -> None:
    a = _FakeStrategy("a", _requirements(live_timeframes=(_5M,)))
    b = _FakeStrategy("b", _requirements(live_timeframes=(_5M, _15M)))
    composed = _compose((a, b))
    await composed.runtime.strategy_manager.start("a", reference=_REF)
    await composed.runtime.strategy_manager.start("b", reference=_REF)
    assert set(composed.runtime.candle_engine.timeframes) == {_5M, _15M}
    composed.runtime.strategy_manager.stop("b")
    assert set(composed.runtime.candle_engine.timeframes) == {_5M}
    composed.runtime.strategy_manager.stop("a")
    assert composed.runtime.candle_engine.timeframes == ()


# --------------------------------------------------------------------------- #
# Session-statistics fact activation (activation ≠ execution)
# --------------------------------------------------------------------------- #
async def test_session_statistics_activation_configures_but_does_not_refresh() -> None:
    strategy = _FakeStrategy("alpha", _requirements(session_statistics_max_age=_5S))
    composed = _compose((strategy,))
    await composed.runtime.strategy_manager.start("alpha", reference=_REF)
    assert composed.runtime.fact_requirements.is_active() is True
    assert composed.runtime.fact_requirements.effective_session_statistics_max_age() == _5S
    assert composed.control.configured[-1] == _5S  # the real coordinator was configured
    assert composed.session_statistics.call_count == 0  # but no refresh_if_due ran


async def test_strictest_freshness_wins_and_relaxes_on_stop() -> None:
    a = _FakeStrategy("a", _requirements(session_statistics_max_age=_10S))
    b = _FakeStrategy("b", _requirements(session_statistics_max_age=_3S))
    composed = _compose((a, b))
    await composed.runtime.strategy_manager.start("a", reference=_REF)
    await composed.runtime.strategy_manager.start("b", reference=_REF)
    assert composed.runtime.fact_requirements.effective_session_statistics_max_age() == _3S
    composed.runtime.strategy_manager.stop("b")
    assert composed.runtime.fact_requirements.effective_session_statistics_max_age() == _10S
    composed.runtime.strategy_manager.stop("a")
    assert composed.runtime.fact_requirements.is_active() is False


# --------------------------------------------------------------------------- #
# Lifecycle retention & release
# --------------------------------------------------------------------------- #
def _mixed_strategy(strategy_id: str) -> _FakeStrategy:
    return _FakeStrategy(
        strategy_id,
        _requirements(
            historical=(HistoricalRequirement(timeframe=_5M, lookback=3),),
            live_timeframes=(_5M,),
            session_statistics_max_age=_5S,
        ),
    )


async def test_pause_retains_all_requirement_types() -> None:
    composed = _compose((_mixed_strategy("alpha"),))
    await composed.runtime.strategy_manager.start("alpha", reference=_REF)
    composed.runtime.strategy_manager.pause("alpha")
    assert _historical_union(composed.runtime) == {(_5M, 3)}
    assert set(composed.runtime.candle_engine.timeframes) == {_5M}
    assert composed.runtime.fact_requirements.is_active() is True


async def test_resume_does_not_re_warm() -> None:
    composed = _compose((_mixed_strategy("alpha"),))
    await composed.runtime.strategy_manager.start("alpha", reference=_REF)
    warmed = composed.historical.call_count
    composed.runtime.strategy_manager.pause("alpha")
    composed.runtime.strategy_manager.resume("alpha")
    assert composed.historical.call_count == warmed  # no re-warm on resume


async def test_stop_releases_only_that_strategy() -> None:
    composed = _compose((_mixed_strategy("a"), _mixed_strategy("b")))
    await composed.runtime.strategy_manager.start("a", reference=_REF)
    await composed.runtime.strategy_manager.start("b", reference=_REF)
    composed.runtime.strategy_manager.stop("a")
    assert _historical_union(composed.runtime) == {(_5M, 3)}  # b survives
    assert set(composed.runtime.candle_engine.timeframes) == {_5M}
    assert composed.runtime.fact_requirements.is_active() is True


async def test_force_stop_releases_requirements() -> None:
    composed = _compose((_mixed_strategy("alpha"),))
    await composed.runtime.strategy_manager.start("alpha", reference=_REF)
    composed.runtime.strategy_manager.force_stop("alpha")
    assert _historical_union(composed.runtime) == set()
    assert composed.runtime.candle_engine.timeframes == ()
    assert composed.runtime.fact_requirements.is_active() is False


async def test_stopped_restart_re_registers_without_duplication() -> None:
    composed = _compose((_mixed_strategy("alpha"),))
    await composed.runtime.strategy_manager.start("alpha", reference=_REF)
    composed.runtime.strategy_manager.stop("alpha")
    await composed.runtime.strategy_manager.start("alpha", reference=_REF)
    assert _historical_union(composed.runtime) == {(_5M, 3)}  # single consumer, no dup
    assert set(composed.runtime.candle_engine.timeframes) == {_5M}


# --------------------------------------------------------------------------- #
# Authority & readiness safety
# --------------------------------------------------------------------------- #
async def test_activation_does_not_enable_authority() -> None:
    composed = _compose((_mixed_strategy("alpha"),))
    await composed.runtime.strategy_manager.start("alpha", reference=_REF)
    status = composed.runtime.status()
    assert status.staged_observation_verified is False
    assert status.tick_aggregate_verified is False


# --------------------------------------------------------------------------- #
# Shared-instance integration: historical install surfaces via ingestion
# --------------------------------------------------------------------------- #
class _OneTickLive:
    def __init__(self, tick: Tick) -> None:
        self._tick = tick
        self.drained = asyncio.Event()
        self._gate = asyncio.Event()

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        yield self._tick
        self.drained.set()
        await self._gate.wait()


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not met in time")


async def test_historical_install_surfaces_via_the_same_tick_engine() -> None:
    instrument = _instrument()
    tick = Tick(instrument=instrument, event_timestamp=_CLOCK, last_price=Decimal("100"))
    live = _OneTickLive(tick)
    strategy = _FakeStrategy(
        "alpha", _requirements(historical=(HistoricalRequirement(timeframe=_5M, lookback=3),))
    )
    composed = _compose((strategy,), live_market_data=live)
    # Warmup installs a HistoricalContext into the shared registry (no version minted).
    await composed.runtime.strategy_manager.start("alpha", reference=_REF)
    state = composed.runtime.registry.get(instrument)
    assert state is not None and state.historical is not None
    # The next accepted live datum surfaces that history in the TickEngine's context.
    await composed.runtime.start()
    await live.drained.wait()
    await _wait_until(lambda: composed.runtime.registry.get(instrument).context is not None)  # type: ignore[union-attr]
    context = composed.runtime.registry.get(instrument).context  # type: ignore[union-attr]
    assert context is not None and context.historical is not None
    await composed.runtime.shutdown()


# --------------------------------------------------------------------------- #
# Full E7 pipeline: refresh → stage → next datum → NOT authoritative (§82)
# --------------------------------------------------------------------------- #
class _ObservingSessionStatsSource(_FakeSessionStatsSource):
    """Returns one session-statistics observation per requested instrument."""

    async def load_session_statistics(
        self, instruments: Sequence[Instrument], *, trading_date: date, observed_at: datetime
    ) -> tuple[SessionStatisticsObservation, ...]:
        self.call_count += 1
        return tuple(
            SessionStatisticsObservation(
                instrument=instrument,
                trading_date=trading_date,
                observed_at=observed_at,
                session_ohlc=ProviderSessionOhlc(
                    open_price=Decimal("100"),
                    high_price=Decimal("105"),
                    low_price=Decimal("98"),
                    close_price=Decimal("101"),
                ),
            )
            for instrument in instruments
        )


async def test_full_pipeline_refresh_stages_but_stays_unavailable() -> None:
    instrument = _instrument()
    tick = Tick(instrument=instrument, event_timestamp=_CLOCK, last_price=Decimal("100"))
    live = _OneTickLive(tick)
    source = _ObservingSessionStatsSource()
    strategy = _FakeStrategy("alpha", _requirements(session_statistics_max_age=_5S))
    composed = _compose((strategy,), live_market_data=live, session_source=source)
    runtime = composed.runtime

    # 1. A strategy activates SESSION_STATISTICS demand → the coordinator is configured.
    await runtime.strategy_manager.start("alpha", reference=_REF)
    assert composed.control.configured[-1] == _5S

    # 2. Drive one LIVE_SESSION refresh opportunity (what the managed driver does) —
    #    one logical batch over the shared universe, staged into the SAME registry.
    refresh = runtime.session_statistics_refresh
    assert refresh is not None
    assert await refresh.refresh_if_due(reference=_CLOCK, trading_date=date(2026, 8, 6)) is True
    assert source.call_count == 1
    staged = runtime.registry.get(instrument)
    assert staged is not None and staged.staged_session_statistics_observation is not None

    # 3. The next accepted live datum surfaces it — but authority is DISABLED, so it never
    #    becomes AUTHORITATIVE (proves the pipeline without bypassing E6C).
    await runtime.start()
    await live.drained.wait()
    await _wait_until(lambda: runtime.registry.get(instrument).context is not None)  # type: ignore[union-attr]
    context = runtime.registry.get(instrument).context  # type: ignore[union-attr]
    assert context is not None
    assert context.session_statistics is None  # staged, consumed, but not authoritative
    assert runtime.status().staged_observation_verified is False
    await runtime.shutdown()
