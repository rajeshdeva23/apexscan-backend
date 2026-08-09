"""DIRECT warmup orchestration, isolation, and determinism (P4.5B; §43)."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
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

_FIVE = Timeframe.minutes(5)
_FIFTEEN = Timeframe.minutes(15)
_SEVEN = Timeframe.minutes(7)
_SESSION = Timeframe.session()
_DIRECT = frozenset({_FIVE, _FIFTEEN, Timeframe.minutes(25), Timeframe.minutes(60), _SESSION})
_REFERENCE = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)
_SCHEDULE = SessionSchedule(
    pre_open_start=datetime(2000, 1, 1, 9, 0).time(),
    opening_auction_start=datetime(2000, 1, 1, 9, 8).time(),
    regular_open=datetime(2000, 1, 1, 9, 15).time(),
    regular_close=datetime(2000, 1, 1, 15, 30).time(),
    closing_end=datetime(2000, 1, 1, 15, 40).time(),
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
    service = HistoricalWarmupService(
        registry=registry, coordinator=coordinator, planner=_planner()
    )
    return service, registry


def _source(**kwargs: object) -> FakeHistoricalSource:
    return FakeHistoricalSource(direct_timeframes=_DIRECT, **kwargs)  # type: ignore[arg-type]


async def test_single_instrument_full_success() -> None:
    source = _source()
    instrument = _instrument()
    service, registry = _service(source, [instrument])
    requirements = (
        HistoricalRequirement(timeframe=_FIVE, lookback=10),
        HistoricalRequirement(timeframe=_SESSION, lookback=3),
    )
    status = await service.warmup([instrument], requirements, reference=_REFERENCE)
    assert status[instrument].state is WarmupState.SATISFIED
    assert set(status[instrument].satisfied) == {_FIVE, _SESSION}
    state = registry.get(instrument)
    assert state is not None
    assert state.historical is not None
    assert {series.timeframe for series in state.historical.series} == {_FIVE, _SESSION}
    assert state.historical.previous_session is not None
    assert state.context is None  # installation mints no MarketContext version


async def test_multiple_instruments_full_success() -> None:
    source = _source()
    instruments = [_instrument("RELIANCE"), _instrument("TCS")]
    service, _ = _service(source, instruments)
    requirements = (HistoricalRequirement(timeframe=_FIVE, lookback=10),)
    status = await service.warmup(instruments, requirements, reference=_REFERENCE)
    assert all(item.state is WarmupState.SATISFIED for item in status.values())


async def test_one_instrument_failure_is_isolated() -> None:
    source = _source(by_symbol={"AAA": Behavior.FAIL})
    instruments = [_instrument("AAA"), _instrument("BBB")]
    service, registry = _service(source, instruments)
    requirements = (HistoricalRequirement(timeframe=_FIVE, lookback=10),)
    status = await service.warmup(instruments, requirements, reference=_REFERENCE)
    assert status[_instrument("AAA")].state is WarmupState.FAILED
    assert status[_instrument("BBB")].state is WarmupState.SATISFIED
    good = registry.get(_instrument("BBB"))
    assert good is not None and good.historical is not None
    assert {series.timeframe for series in good.historical.series} == {_FIVE}


async def test_partial_series_success() -> None:
    source = _source(by_interval={timedelta(minutes=15): Behavior.FAIL})
    instrument = _instrument()
    service, registry = _service(source, [instrument])
    requirements = (
        HistoricalRequirement(timeframe=_FIVE, lookback=10),
        HistoricalRequirement(timeframe=_FIFTEEN, lookback=10),
        HistoricalRequirement(timeframe=_SESSION, lookback=3),
    )
    status = await service.warmup([instrument], requirements, reference=_REFERENCE)
    assert status[instrument].state is WarmupState.PARTIAL
    assert set(status[instrument].unresolved) == {_FIFTEEN}
    state = registry.get(instrument)
    assert state is not None and state.historical is not None
    assert {series.timeframe for series in state.historical.series} == {_FIVE, _SESSION}


async def test_direct_and_reconstruction_pending_mix() -> None:
    source = _source()
    instrument = _instrument()
    service, registry = _service(source, [instrument])
    requirements = (
        HistoricalRequirement(timeframe=_FIVE, lookback=10),
        HistoricalRequirement(timeframe=_SEVEN, lookback=5),
        HistoricalRequirement(timeframe=_SESSION, lookback=3),
    )
    status = await service.warmup([instrument], requirements, reference=_REFERENCE)
    assert status[instrument].state is WarmupState.SATISFIED
    assert set(status[instrument].pending_reconstruction) == {_SEVEN}
    state = registry.get(instrument)
    assert state is not None and state.historical is not None
    assert {series.timeframe for series in state.historical.series} == {_FIVE, _SESSION}
    assert source.call_count == 2  # 5m + session only; no request for 7m


async def test_lookback_trimming_keeps_most_recent_n() -> None:
    source = _source()
    instrument = _instrument()
    service, registry = _service(source, [instrument])
    status = await service.warmup(
        [instrument], (HistoricalRequirement(timeframe=_FIVE, lookback=10),), reference=_REFERENCE
    )
    assert status[instrument].state is WarmupState.SATISFIED
    state = registry.get(instrument)
    assert state is not None and state.historical is not None
    series = next(s for s in state.historical.series if s.timeframe == _FIVE)
    assert len(series.candles) == 10


async def test_previous_session_facts_reuse_the_session_series() -> None:
    source = _source()
    instrument = _instrument()
    service, registry = _service(source, [instrument])
    await service.warmup(
        [instrument], (HistoricalRequirement(timeframe=_SESSION, lookback=3),), reference=_REFERENCE
    )
    assert source.call_count == 1  # no extra request just for previous-session facts
    state = registry.get(instrument)
    assert state is not None and state.historical is not None
    session_series = state.historical.series[0]
    assert state.historical.previous_session is not None
    assert state.historical.previous_session.candle == session_series.candles[-1]


async def test_next_accepted_tick_surfaces_warmed_history() -> None:
    source = _source()
    instrument = _instrument()
    service, registry = _service(source, [instrument])
    await service.warmup(
        [instrument], (HistoricalRequirement(timeframe=_FIVE, lookback=10),), reference=_REFERENCE
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
    tick = Tick(
        instrument=instrument,
        event_timestamp=datetime(2026, 8, 10, 6, 30, tzinfo=UTC),
        last_price=Decimal("100"),
        traded_quantity=1,
    )
    result = engine.process(tick)
    installed = registry.get(instrument)
    assert result.context is not None
    assert installed is not None
    assert result.context.historical is installed.historical


async def test_assembly_is_deterministic() -> None:
    requirements = (
        HistoricalRequirement(timeframe=_FIVE, lookback=10),
        HistoricalRequirement(timeframe=_SESSION, lookback=3),
    )
    instrument = _instrument()

    service_a, registry_a = _service(_source(), [instrument])
    await service_a.warmup([instrument], requirements, reference=_REFERENCE)
    service_b, registry_b = _service(_source(), [instrument])
    await service_b.warmup([instrument], requirements, reference=_REFERENCE)

    context_a = registry_a.get(instrument)
    context_b = registry_b.get(instrument)
    assert context_a is not None and context_b is not None
    assert context_a.historical == context_b.historical


async def test_not_started_before_warmup() -> None:
    service, _ = _service(_source(), [_instrument()])
    assert service.status_for(_instrument()).state is WarmupState.NOT_STARTED


async def test_warming_state_is_observable_mid_flight() -> None:
    source = _source(default=Behavior.BLOCK)
    instrument = _instrument()
    service, _ = _service(source, [instrument])
    task = asyncio.create_task(
        service.warmup(
            [instrument],
            (HistoricalRequirement(timeframe=_FIVE, lookback=10),),
            reference=_REFERENCE,
        )
    )
    await source.wait_until_active(1)
    assert service.status_for(instrument).state is WarmupState.WARMING
    source.release_all()
    await task
    assert service.status_for(instrument).state is WarmupState.SATISFIED
