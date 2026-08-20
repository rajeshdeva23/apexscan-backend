"""LiveMarketRuntime skeleton & shared-core composition (RUN-A; ADR-010).

Proves the broker-neutral runtime composes one shared EventBus / registry /
CandleEngine / TickEngine / StrategyManager, subscribes the manager before any
ingestion, boots with zero strategies and zero timeframes, keeps both
session-statistics authority bits disabled, and pulls in no provider I/O.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import app.services.market_runtime as market_runtime
from app.core.config import Settings
from app.events.bus import Event, EventBus
from app.market_engine.clock import ManualClock
from app.market_engine.context import MarketContext, MarketState, SessionContext
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.historical.context import (
    HistoricalContext,
    HistoricalSeries,
    PreviousSessionFacts,
)
from app.market_engine.historical.requirements import HistoricalRequirement
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, Instrument, ProviderStatus, Tick
from app.services.cross_instrument_scanner import ScannerOrdering, ScannerRankingPolicy
from app.services.market_runtime import (
    LiveMarketRuntime,
    RuntimeLifecycleError,
    RuntimeRequirementContext,
    RuntimeRequirements,
    RuntimeState,
)
from app.strategies.enums import EvaluationStatus
from app.strategies.implementations.narrow_cpr import NarrowCprConfiguration, NarrowCprStrategy
from app.strategies.results import MetricEntry, StrategyResult
from app.strategy_manager.events import StrategyResultsPublished
from app.strategy_manager.requirements_bridge import RequirementsCoordinator

_NOW = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)  # 12:00 IST — a live-session instant
_ERROR_THRESHOLD = 3


def _settings() -> Settings:
    return Settings(
        app_env="development",
        database_url="postgresql+asyncpg://user:pass@localhost:5432/apexscan",
        redis_url="redis://localhost:6379/0",
    )


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _runtime(*, instruments: tuple[Instrument, ...] = ()) -> LiveMarketRuntime:
    return LiveMarketRuntime(
        settings=_settings(),
        error_threshold=_ERROR_THRESHOLD,
        instruments=instruments,
        clock=ManualClock(_NOW),
        sequence=MonotonicSequence(),
    )


_TD = date(2026, 8, 6)
_CPR_POLICY = ScannerRankingPolicy("narrow_cpr", "cpr_width_pct", ScannerOrdering.ASCENDING)


def _scanner_runtime() -> LiveMarketRuntime:
    return LiveMarketRuntime(
        settings=_settings(),
        error_threshold=_ERROR_THRESHOLD,
        instruments=(_instrument("AAA"), _instrument("BBB")),
        scanner_ranking_policies=(_CPR_POLICY,),
        clock=ManualClock(_NOW),
        sequence=MonotonicSequence(),
    )


def _cpr_event(symbol: str, width: str) -> StrategyResultsPublished:
    instrument = _instrument(symbol)
    result = StrategyResult(
        strategy_id="narrow_cpr",
        strategy_version="1.0.0",
        config_version="1.0.0",
        instrument=instrument,
        context_version=1,
        evaluation_timestamp=_NOW,
        status=EvaluationStatus.MATCHED,
        reason_codes=("NARROW_CPR_VALID",),
        metrics=(MetricEntry(name="cpr_width_pct", value=Decimal(width)),),
    )
    return StrategyResultsPublished(
        instrument=instrument, context_version=1, results=(result,), ranked=(), trading_date=_TD
    )


def _tick(instrument: Instrument, *, at: datetime = _NOW) -> Tick:
    return Tick(instrument=instrument, event_timestamp=at, last_price=Decimal("100"))


def _recorder(bus: EventBus, event_type: type[Event]) -> list[Event]:
    recorded: list[Event] = []
    bus.subscribe(event_type, recorded.append)
    return recorded


# --------------------------------------------------------------------------- #
# Construction & provider-free boundary
# --------------------------------------------------------------------------- #
def test_runtime_constructs_without_a_provider() -> None:
    runtime = _runtime()
    assert runtime.status().state is RuntimeState.NOT_STARTED


def test_runtime_source_stays_provider_blind() -> None:
    # The runtime consumes a broker-neutral live-data Protocol; it must never reference
    # a concrete provider, provider identifiers, or provider transport (ADR-010 D4; §56).
    source = Path(market_runtime.__file__).read_text()
    for forbidden in (
        "app.adapters.dhan",
        "dhan",
        "DhanRestAdapter",
        "security_id",
        "exchange_segment",
        "marketfeed",
        "httpx",
        "load_instruments",
        "load_fno_stock_universe",
        "staged_observation_verified=True",
        "tick_aggregate_verified=True",
        "provider_aggregate_verified",
        "OpenHigh",
        "OpenLow",
        "OPEN_HIGH",
        "OPEN_LOW",
        "datetime.now",
        "date.today",
    ):
        assert forbidden not in source, f"provider-specific token leaked into runtime: {forbidden}"


def test_invalid_error_threshold_is_rejected() -> None:
    with pytest.raises(ValueError, match="error_threshold"):
        LiveMarketRuntime(settings=_settings(), error_threshold=0)


# --------------------------------------------------------------------------- #
# Zero-instrument / zero-timeframe
# --------------------------------------------------------------------------- #
def test_empty_instrument_registry_is_accepted() -> None:
    runtime = _runtime()
    assert runtime.status().known_instrument_count == 0
    assert runtime.registry.is_known(_instrument()) is False


def test_candle_engine_starts_with_zero_timeframes() -> None:
    runtime = _runtime()
    assert runtime.candle_engine.timeframes == ()
    assert runtime.status().active_timeframe_count == 0


# --------------------------------------------------------------------------- #
# Shared-instance invariants (ADR-010 D3)
# --------------------------------------------------------------------------- #
def test_tick_engine_writes_into_the_runtime_registry() -> None:
    instrument = _instrument()
    runtime = _runtime(instruments=(instrument,))
    result = runtime.tick_engine.process(_tick(instrument))
    assert result.context is not None
    state = runtime.registry.get(instrument)
    assert state is not None and state.context is not None  # same registry the engine wrote to


def test_tick_engine_publishes_on_the_runtime_bus() -> None:
    instrument = _instrument()
    runtime = _runtime(instruments=(instrument,))
    recorded = _recorder(runtime.bus, MarketContextCreated)
    runtime.tick_engine.process(_tick(instrument))
    assert [type(event) for event in recorded] == [MarketContextCreated]


async def test_manager_subscribes_to_the_runtime_bus_on_start() -> None:
    runtime = _runtime()
    assert len(runtime.bus._subscribers.get(MarketContextCreated, [])) == 0  # noqa: SLF001
    await runtime.start()
    after_created = len(runtime.bus._subscribers.get(MarketContextCreated, []))  # noqa: SLF001
    after_updated = len(runtime.bus._subscribers.get(MarketContextUpdated, []))  # noqa: SLF001
    assert after_created == 1 and after_updated == 1  # the manager, on the runtime's own bus


def test_repeated_property_access_returns_stable_single_instances() -> None:
    runtime = _runtime()
    assert runtime.bus is runtime.bus
    assert runtime.registry is runtime.registry
    assert runtime.tick_engine is runtime.tick_engine
    assert runtime.strategy_manager is runtime.strategy_manager


# --------------------------------------------------------------------------- #
# Zero-strategy / zero-demand behavior
# --------------------------------------------------------------------------- #
async def test_zero_strategies_route_nothing_and_publish_nothing() -> None:
    instrument = _instrument()
    runtime = _runtime(instruments=(instrument,))
    await runtime.start()
    results = _recorder(runtime.bus, StrategyResultsPublished)
    runtime.tick_engine.process(_tick(instrument))
    assert runtime.strategy_manager.evaluations_for(instrument) == ()
    assert results == []  # no StrategyResultsPublished with zero strategies


def test_requirement_registries_start_empty_and_inactive() -> None:
    runtime = _runtime()
    assert runtime.historical_requirements.effective_requirements() == ()
    assert len(runtime.live_timeframe_requirements.effective_timeframes()) == 0
    assert runtime.fact_requirements.is_active() is False
    assert runtime.fact_requirements.effective_session_statistics_max_age() is None


# --------------------------------------------------------------------------- #
# Authority safety (E6A / E6B — both disabled)
# --------------------------------------------------------------------------- #
def test_production_authority_bits_are_disabled() -> None:
    runtime = _runtime()
    assert runtime.authority.staged_observation_verified is False
    assert runtime.authority.tick_aggregate_verified is False
    status = runtime.status()
    assert status.staged_observation_verified is False
    assert status.tick_aggregate_verified is False


async def test_wiring_does_not_make_a_session_ohlc_tick_authoritative() -> None:
    instrument = _instrument()
    runtime = _runtime(instruments=(instrument,))
    await runtime.start()
    # Even a tick carrying a session OHLC aggregate cannot establish authority: the
    # tick-aggregate source is unverified in production (E6A/E6B).
    from app.schemas.market_data import ProviderSessionOhlc

    tick = Tick(
        instrument=instrument,
        event_timestamp=_NOW,
        last_price=Decimal("100"),
        session_ohlc=ProviderSessionOhlc(
            open_price=Decimal("100"),
            high_price=Decimal("105"),
            low_price=Decimal("98"),
            close_price=Decimal("101"),
        ),
    )
    result = runtime.tick_engine.process(tick)
    assert result.context is not None
    assert result.context.session_statistics is None  # never AUTHORITATIVE


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
async def test_start_subscribes_the_manager() -> None:
    runtime = _runtime()
    await runtime.start()
    assert runtime.manager_subscribed is True
    assert runtime.status().state is RuntimeState.STARTED


async def test_repeated_start_is_idempotent() -> None:
    runtime = _runtime()
    await runtime.start()
    await runtime.start()  # no duplicate subscription
    assert len(runtime.bus._subscribers.get(MarketContextCreated, [])) == 1  # noqa: SLF001


async def test_shutdown_unsubscribes_the_manager() -> None:
    runtime = _runtime()
    await runtime.start()
    await runtime.shutdown()
    assert runtime.manager_subscribed is False
    assert runtime.status().state is RuntimeState.SHUTDOWN
    assert len(runtime.bus._subscribers.get(MarketContextCreated, [])) == 0  # noqa: SLF001


async def test_events_after_shutdown_do_not_route_to_the_manager() -> None:
    instrument = _instrument()
    runtime = _runtime(instruments=(instrument,))
    await runtime.start()
    await runtime.shutdown()
    # The engine still publishes, but the (unsubscribed) manager records nothing.
    runtime.tick_engine.process(_tick(instrument))
    assert runtime.strategy_manager.evaluations_for(instrument) == ()


async def test_shutdown_is_idempotent() -> None:
    runtime = _runtime()
    await runtime.start()
    await runtime.shutdown()
    await runtime.shutdown()  # safe
    assert runtime.status().state is RuntimeState.SHUTDOWN


async def test_restart_after_shutdown_is_rejected() -> None:
    runtime = _runtime()
    await runtime.start()
    await runtime.shutdown()
    with pytest.raises(RuntimeLifecycleError, match="cannot restart"):
        await runtime.start()


# --------------------------------------------------------------------------- #
# Health (truthful skeleton semantics — never claims a provider is connected)
# --------------------------------------------------------------------------- #
async def test_health_is_unknown_before_start() -> None:
    runtime = _runtime()
    health = await runtime.verify_health()
    assert health.status is ProviderStatus.UNKNOWN


async def test_health_is_unknown_after_start() -> None:
    runtime = _runtime()
    await runtime.start()
    health = await runtime.verify_health()
    assert health.status is ProviderStatus.UNKNOWN  # no provider wired in RUN-A


async def test_health_is_unknown_after_shutdown() -> None:
    runtime = _runtime()
    await runtime.start()
    await runtime.shutdown()
    health = await runtime.verify_health()
    assert health.status is ProviderStatus.UNKNOWN


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
async def test_status_snapshot_is_deterministic() -> None:
    first = _runtime()
    second = _runtime()
    await first.start()
    await second.start()
    assert first.status() == second.status()


async def test_verify_health_uses_the_injected_clock() -> None:
    runtime = _runtime()
    health = await runtime.verify_health()
    assert health.observed_at == _NOW  # deterministic; no wall-clock read


# --------------------------------------------------------------------------- #
# Cross-instrument scanner (ADR-012) — subscribed passively, no task
# --------------------------------------------------------------------------- #
async def test_scanner_is_subscribed_and_ranks_after_start() -> None:
    runtime = _scanner_runtime()
    await runtime.start()
    runtime.bus.publish(_cpr_event("AAA", "0.03"))
    runtime.bus.publish(_cpr_event("BBB", "0.01"))
    snapshot = runtime.scanner_snapshot("narrow_cpr")
    assert snapshot is not None
    assert [candidate.instrument.symbol for candidate in snapshot.candidates] == ["BBB", "AAA"]
    # The scanner is a passive subscriber — it starts no managed task.
    status = runtime.status()
    assert not status.ingestion_running
    assert not status.refresh_driver_running
    assert not status.calendar_monitor_running
    await runtime.shutdown()


async def test_scanner_unsubscribes_on_shutdown() -> None:
    runtime = _scanner_runtime()
    await runtime.start()
    runtime.bus.publish(_cpr_event("AAA", "0.05"))
    before = runtime.scanner_snapshot("narrow_cpr")
    await runtime.shutdown()
    runtime.bus.publish(_cpr_event("BBB", "0.01"))  # detached: must not enter the snapshot
    after = runtime.scanner_snapshot("narrow_cpr")
    assert before is not None
    assert before == after
    assert [candidate.instrument.symbol for candidate in before.candidates] == ["AAA"]


# --------------------------------------------------------------------------- #
# Production strategy registration & enablement (ADR-013) — end-to-end
# --------------------------------------------------------------------------- #
class _FakeSink:
    def set_required_timeframes(self, timeframes: frozenset[Timeframe]) -> None:
        pass


class _SatisfyingWarmup:
    async def warmup(
        self,
        instruments: tuple[Instrument, ...],
        effective_requirements: tuple[HistoricalRequirement, ...],
        *,
        reference: datetime,
    ) -> dict[Instrument, frozenset[Timeframe]]:
        # Satisfy the session timeframe for every universe instrument (no provider I/O).
        return {instrument: frozenset({Timeframe.session()}) for instrument in instruments}


def _requirements_factory(context: RuntimeRequirementContext) -> RuntimeRequirements:
    coordinator = RequirementsCoordinator(
        instruments=context.instruments,
        historical=context.historical_requirements,
        live=context.live_timeframe_requirements,
        sink=_FakeSink(),
        warmup=_SatisfyingWarmup(),
    )
    return RuntimeRequirements(coordinator=coordinator, session_statistics_refresh=None)


def _strategy_runtime(*, autostart: tuple[str, ...] = ("narrow_cpr",)) -> LiveMarketRuntime:
    return LiveMarketRuntime(
        settings=_settings(),
        error_threshold=_ERROR_THRESHOLD,
        instruments=(_instrument("AAA"), _instrument("BBB")),
        strategies=(NarrowCprStrategy(),),
        configurations={"narrow_cpr": NarrowCprConfiguration(config_version="1.0.0")},
        requirements_factory=_requirements_factory,
        scanner_ranking_policies=(_CPR_POLICY,),
        autostart_strategy_ids=autostart,
        clock=ManualClock(_NOW),
        sequence=MonotonicSequence(),
    )


def _strategy_candle(instrument: Instrument, close: str) -> Candle:
    # sum(H,L,C)=300 ⇒ pivot=100 ⇒ cpr_width_pct = |close - 100| (H/L split is irrelevant).
    close_price = Decimal(close)
    return Candle(
        instrument=instrument,
        start_timestamp=_NOW,
        end_timestamp=_NOW + timedelta(hours=6),
        open_price=Decimal("100"),
        high_price=Decimal("140"),
        low_price=Decimal("160") - close_price,
        close_price=close_price,
        traded_quantity=100,
    )


def _strategy_context(symbol: str, close: str) -> MarketContext:
    instrument = _instrument(symbol)
    candle = _strategy_candle(instrument, close)
    historical = HistoricalContext(
        instrument=instrument,
        previous_session=PreviousSessionFacts(trading_date=_TD, candle=candle),
        series=(HistoricalSeries(timeframe=Timeframe.session(), candles=(candle,)),),
    )
    return MarketContext(
        instrument=instrument,
        version=1,
        sequence=1,
        event_timestamp=_NOW,
        observed_at=_NOW,
        session=SessionContext(
            trading_date=_TD,
            market_state=MarketState.LIVE_SESSION,
            exchange_timezone="Asia/Kolkata",
        ),
        historical=historical,
    )


async def test_enabled_strategy_starts_and_ranks_end_to_end() -> None:
    runtime = _strategy_runtime()
    await runtime.start()
    # REG9: starting the enabled strategy put its session requirement into the effective union.
    reqs = runtime.historical_requirements.effective_requirements()
    assert HistoricalRequirement(timeframe=Timeframe.session(), lookback=1) in reqs
    # Only a RUNNING strategy evaluates → a ranked snapshot proves narrow_cpr reached RUNNING
    # and the warmup→evaluate→result→scanner pipeline operated. Widths: BBB 4 < AAA 12.
    runtime.bus.publish(MarketContextCreated(context=_strategy_context("AAA", "112")))
    runtime.bus.publish(MarketContextCreated(context=_strategy_context("BBB", "104")))
    snapshot = runtime.scanner_snapshot("narrow_cpr")
    assert snapshot is not None
    assert [candidate.instrument.symbol for candidate in snapshot.candidates] == ["BBB", "AAA"]
    # No managed task is added by enablement (ingestion/refresh/monitor unwired here).
    status = runtime.status()
    assert not (status.ingestion_running or status.refresh_driver_running)
    await runtime.shutdown()


async def test_strategy_start_failure_is_isolated() -> None:
    # 'ghost' is autostarted but not registered → its start raises and is isolated; narrow_cpr
    # (also autostarted, registered) still reaches RUNNING and ranks (ADR-013 REG6).
    runtime = _strategy_runtime(autostart=("ghost", "narrow_cpr"))
    await runtime.start()  # completes despite the ghost failure
    runtime.bus.publish(MarketContextCreated(context=_strategy_context("AAA", "112")))
    snapshot = runtime.scanner_snapshot("narrow_cpr")
    assert snapshot is not None
    assert snapshot.eligible_count == 1
    await runtime.shutdown()
