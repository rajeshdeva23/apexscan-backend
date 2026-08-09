"""Dormant activation: zero-requirement, refresh, and removal behavior (P4.5E; §16-21)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from app.market_engine.candle_engine import CandleEngine
from app.market_engine.historical.cache import HistoricalCache
from app.market_engine.historical.calendar_window import CalendarCoverage, HistoricalCalendarWindow
from app.market_engine.historical.coordinator import HistoricalCoordinator
from app.market_engine.historical.requirements import HistoricalRequirement
from app.market_engine.historical.service import (
    HistoricalRangePlanner,
    HistoricalWarmupService,
    WarmupState,
)
from app.market_engine.session import SessionSchedule, TradingCalendar
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument
from tests.fakes.historical_source import FakeHistoricalSource

_TZ = "Asia/Kolkata"
_REFERENCE = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)
_FIVE = Timeframe.minutes(5)
_FIFTEEN = Timeframe.minutes(15)
_DIRECT = frozenset({_FIVE, _FIFTEEN, Timeframe.session()})
_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)


def _instrument() -> Instrument:
    return Instrument(exchange="NSE", symbol="RELIANCE")


def _service(
    source: FakeHistoricalSource,
) -> tuple[HistoricalWarmupService, InstrumentStateRegistry]:
    window = HistoricalCalendarWindow(
        calendar=TradingCalendar(),
        coverage=CalendarCoverage(start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)),
    )
    planner = HistoricalRangePlanner(
        schedule=_SCHEDULE, exchange_timezone=_TZ, calendar_window=window
    )
    coordinator = HistoricalCoordinator(source=source, cache=HistoricalCache(), max_concurrency=4)
    registry = InstrumentStateRegistry([_instrument()])
    engine = CandleEngine(schedule=_SCHEDULE, exchange_timezone=_TZ, timeframes=[_FIVE])
    service = HistoricalWarmupService(
        registry=registry, coordinator=coordinator, planner=planner, candles=engine
    )
    return service, registry


def _timeframes(registry: InstrumentStateRegistry) -> set[Timeframe]:
    state = registry.get(_instrument())
    assert state is not None and state.historical is not None
    return {series.timeframe for series in state.historical.series}


async def test_zero_requirements_makes_no_source_call() -> None:
    source = FakeHistoricalSource(direct_timeframes=_DIRECT)
    service, registry = _service(source)
    status = await service.warmup([_instrument()], (), reference=_REFERENCE)
    assert source.call_count == 0  # dormant — nothing fetched
    assert status[_instrument()].state is WarmupState.SATISFIED  # vacuously satisfied
    state = registry.get(_instrument())
    assert state is not None and state.historical is not None
    assert state.historical.series == ()  # explicitly-empty snapshot installed


async def test_requirement_refresh_installs_new_snapshot() -> None:
    source = FakeHistoricalSource(direct_timeframes=_DIRECT)
    service, registry = _service(source)
    await service.warmup(
        [_instrument()],
        (HistoricalRequirement(timeframe=_FIVE, lookback=20),),
        reference=_REFERENCE,
    )
    assert _timeframes(registry) == {_FIVE}

    await service.warmup(
        [_instrument()],
        (
            HistoricalRequirement(timeframe=_FIVE, lookback=100),
            HistoricalRequirement(timeframe=_FIFTEEN, lookback=20),
        ),
        reference=_REFERENCE,
    )
    assert _timeframes(registry) == {_FIVE, _FIFTEEN}


async def test_requirement_removal_shrinks_snapshot() -> None:
    source = FakeHistoricalSource(direct_timeframes=_DIRECT)
    service, registry = _service(source)
    await service.warmup(
        [_instrument()],
        (
            HistoricalRequirement(timeframe=_FIVE, lookback=100),
            HistoricalRequirement(timeframe=_FIFTEEN, lookback=50),
        ),
        reference=_REFERENCE,
    )
    assert _timeframes(registry) == {_FIVE, _FIFTEEN}

    await service.warmup(
        [_instrument()],
        (HistoricalRequirement(timeframe=_FIVE, lookback=20),),
        reference=_REFERENCE,
    )
    assert _timeframes(registry) == {_FIVE}  # obsolete 15m series dropped
