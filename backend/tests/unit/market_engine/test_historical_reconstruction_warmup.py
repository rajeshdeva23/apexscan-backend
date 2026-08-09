"""Reconstruction integrated into warmup: cache reuse, status, determinism (P4.5C; §38-39)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from app.events.bus import Event, EventBus
from app.market_engine.clock import ManualClock
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.historical.cache import HistoricalCache
from app.market_engine.historical.calendar_window import CalendarCoverage, HistoricalCalendarWindow
from app.market_engine.historical.coordinator import HistoricalCoordinator
from app.market_engine.historical.requirements import HistoricalRequirement
from app.market_engine.historical.service import (
    HistoricalRangePlanner,
    HistoricalWarmupService,
    WarmupState,
)
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.session import SessionSchedule, TradingCalendar
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.tick_engine import TickEngine
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument, Tick
from tests.fakes.historical_source import Behavior, FakeHistoricalSource

_ONE = Timeframe.minutes(1)
_FIVE = Timeframe.minutes(5)
_SEVEN = Timeframe.minutes(7)
_FOURTEEN = Timeframe.minutes(14)
_FIFTEEN = Timeframe.minutes(15)
_SESSION = Timeframe.session()
_DIRECT = frozenset({_ONE, _FIVE, _FIFTEEN, _SESSION})
_REFERENCE = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)
_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _planner() -> HistoricalRangePlanner:
    window = HistoricalCalendarWindow(
        calendar=TradingCalendar(),
        coverage=CalendarCoverage(start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)),
    )
    return HistoricalRangePlanner(
        schedule=_SCHEDULE, exchange_timezone="Asia/Kolkata", calendar_window=window
    )


def _service(
    source: FakeHistoricalSource, instruments: list[Instrument]
) -> tuple[HistoricalWarmupService, InstrumentStateRegistry]:
    registry = InstrumentStateRegistry(instruments)
    coordinator = HistoricalCoordinator(source=source, cache=HistoricalCache(), max_concurrency=4)
    return (
        HistoricalWarmupService(registry=registry, coordinator=coordinator, planner=_planner()),
        registry,
    )


def _source(**kwargs: object) -> FakeHistoricalSource:
    return FakeHistoricalSource(direct_timeframes=_DIRECT, **kwargs)  # type: ignore[arg-type]


def _timeframes(registry: InstrumentStateRegistry, instrument: Instrument) -> set[Timeframe]:
    state = registry.get(instrument)
    assert state is not None and state.historical is not None
    return {series.timeframe for series in state.historical.series}


async def test_direct_and_reconstructed_success() -> None:
    instrument = _instrument()
    service, registry = _service(_source(), [instrument])
    requirements = (
        HistoricalRequirement(timeframe=_FIVE, lookback=10),
        HistoricalRequirement(timeframe=_SEVEN, lookback=5),
        HistoricalRequirement(timeframe=_SESSION, lookback=3),
    )
    status = await service.warmup([instrument], requirements, reference=_REFERENCE)
    assert status[instrument].state is WarmupState.SATISFIED
    assert set(status[instrument].satisfied) == {_FIVE, _SEVEN, _SESSION}
    assert status[instrument].pending_reconstruction == ()
    assert _timeframes(registry, instrument) == {_FIVE, _SEVEN, _SESSION}


async def test_pending_seven_minute_becomes_satisfied() -> None:
    instrument = _instrument()
    service, _ = _service(_source(), [instrument])
    status = await service.warmup(
        [instrument], (HistoricalRequirement(timeframe=_SEVEN, lookback=5),), reference=_REFERENCE
    )
    assert _SEVEN in status[instrument].satisfied
    assert _SEVEN not in status[instrument].pending_reconstruction


async def test_reconstruction_failure_is_unresolved_not_pending() -> None:
    source = _source(by_interval={timedelta(minutes=1): Behavior.INSUFFICIENT})
    instrument = _instrument()
    service, registry = _service(source, [instrument])
    requirements = (
        HistoricalRequirement(timeframe=_FIVE, lookback=10),
        HistoricalRequirement(timeframe=_SEVEN, lookback=5),
    )
    status = await service.warmup([instrument], requirements, reference=_REFERENCE)
    assert status[instrument].state is WarmupState.PARTIAL
    assert set(status[instrument].unresolved) == {_SEVEN}
    assert status[instrument].pending_reconstruction == ()
    assert _timeframes(registry, instrument) == {_FIVE}  # partial context preserved


async def test_direct_target_short_circuits_reconstruction() -> None:
    # 1m fails; if 15m were (wrongly) reconstructed from 1m it would fail. Direct wins.
    source = _source(by_interval={timedelta(minutes=1): Behavior.FAIL})
    instrument = _instrument()
    service, _ = _service(source, [instrument])
    status = await service.warmup(
        [instrument], (HistoricalRequirement(timeframe=_FIFTEEN, lookback=5),), reference=_REFERENCE
    )
    assert status[instrument].state is WarmupState.SATISFIED
    assert source.calls_by_interval.get(timedelta(minutes=1), 0) == 0  # 1m never fetched


async def test_base_coverage_reused_on_second_warmup() -> None:
    source = _source()
    instrument = _instrument()
    service, _ = _service(source, [instrument])
    requirements = (HistoricalRequirement(timeframe=_SEVEN, lookback=5),)
    await service.warmup([instrument], requirements, reference=_REFERENCE)
    first_calls = source.call_count
    await service.warmup([instrument], requirements, reference=_REFERENCE)
    assert source.call_count == first_calls  # fully served from cache


async def test_shared_base_is_fetched_once() -> None:
    source = _source()
    instrument = _instrument()
    service, _ = _service(source, [instrument])
    requirements = (
        HistoricalRequirement(timeframe=_SEVEN, lookback=10),
        HistoricalRequirement(timeframe=_FOURTEEN, lookback=10),
    )
    await service.warmup([instrument], requirements, reference=_REFERENCE)
    assert source.calls_by_interval[timedelta(minutes=1)] == 1  # one shared 1m base fetch


async def test_reconstructed_series_trimmed_to_lookback() -> None:
    instrument = _instrument()
    service, registry = _service(_source(), [instrument])
    await service.warmup(
        [instrument], (HistoricalRequirement(timeframe=_SEVEN, lookback=5),), reference=_REFERENCE
    )
    state = registry.get(instrument)
    assert state is not None and state.historical is not None
    series = next(s for s in state.historical.series if s.timeframe == _SEVEN)
    assert len(series.candles) == 5


async def test_install_creates_no_version_or_event() -> None:
    instrument = _instrument()
    service, registry = _service(_source(), [instrument])
    await service.warmup(
        [instrument], (HistoricalRequirement(timeframe=_SEVEN, lookback=5),), reference=_REFERENCE
    )
    state = registry.get(instrument)
    assert state is not None
    assert state.context is None


async def test_next_accepted_tick_surfaces_reconstructed_history() -> None:
    instrument = _instrument()
    service, registry = _service(_source(), [instrument])
    await service.warmup(
        [instrument], (HistoricalRequirement(timeframe=_SEVEN, lookback=5),), reference=_REFERENCE
    )
    bus = EventBus()
    recorded: list[Event] = []
    bus.subscribe(MarketContextCreated, recorded.append)
    bus.subscribe(MarketContextUpdated, recorded.append)
    engine = TickEngine(
        registry=registry,
        bus=bus,
        clock=ManualClock(datetime(2026, 8, 10, 7, 0, tzinfo=UTC)),
        sequence=MonotonicSequence(),
    )
    result = engine.process(
        Tick(
            instrument=instrument,
            event_timestamp=datetime(2026, 8, 10, 6, 30, tzinfo=UTC),
            last_price=Decimal("100"),
            traded_quantity=1,
        )
    )
    assert result.context is not None
    assert result.context.historical is not None
    assert _SEVEN in {series.timeframe for series in result.context.historical.series}


async def test_reconstruction_assembly_is_deterministic() -> None:
    instrument = _instrument()
    requirements = (
        HistoricalRequirement(timeframe=_FIVE, lookback=10),
        HistoricalRequirement(timeframe=_SEVEN, lookback=5),
    )
    service_a, registry_a = _service(_source(), [instrument])
    await service_a.warmup([instrument], requirements, reference=_REFERENCE)
    service_b, registry_b = _service(_source(), [instrument])
    await service_b.warmup([instrument], requirements, reference=_REFERENCE)
    context_a = registry_a.get(instrument)
    context_b = registry_b.get(instrument)
    assert context_a is not None and context_b is not None
    assert context_a.historical == context_b.historical
