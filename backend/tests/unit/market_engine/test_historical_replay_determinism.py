"""Atomic surfacing, replay determinism, and immutability for history (P4.5E; §3,24-28)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.events.bus import Event, EventBus
from app.market_engine.candle_engine import CandleEngine
from app.market_engine.clock import ManualClock
from app.market_engine.context import MarketState, SessionContext
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.historical.cache import HistoricalCache
from app.market_engine.historical.calendar_window import CalendarCoverage, HistoricalCalendarWindow
from app.market_engine.historical.coordinator import HistoricalCoordinator
from app.market_engine.historical.reconciliation import ReconciliationOutcome
from app.market_engine.historical.requirements import (
    HistoricalRequirement,
    HistoricalRequirementRegistry,
)
from app.market_engine.historical.service import HistoricalRangePlanner, HistoricalWarmupService
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.session import MarketSessionClassifier, SessionSchedule, TradingCalendar
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.tick_engine import TickEngine
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument, Tick
from tests.fakes.historical_source import FakeHistoricalSource

_IST = ZoneInfo("Asia/Kolkata")
_TZ = "Asia/Kolkata"
_PRIOR = date(2026, 8, 6)
_REFERENCE = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)
_FIVE = Timeframe.minutes(5)
_SEVEN = Timeframe.minutes(7)
_ONE = Timeframe.minutes(1)
_DIRECT = frozenset({_ONE, _FIVE, Timeframe.minutes(15), Timeframe.session()})
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
    return HistoricalRangePlanner(schedule=_SCHEDULE, exchange_timezone=_TZ, calendar_window=window)


def _tick(day: date, hour: int, minute: int) -> Tick:
    return Tick(
        instrument=_instrument(),
        event_timestamp=datetime.combine(day, time(hour, minute), tzinfo=_IST),
        last_price=Decimal("100"),
        traded_quantity=1,
        session_cumulative_volume=100,
    )


def _wired() -> tuple[
    TickEngine, HistoricalWarmupService, CandleEngine, InstrumentStateRegistry, list[Event]
]:
    engine = CandleEngine(schedule=_SCHEDULE, exchange_timezone=_TZ, timeframes=[_FIVE])
    registry = InstrumentStateRegistry([_instrument()])
    bus = EventBus()
    recorded: list[Event] = []
    bus.subscribe(MarketContextCreated, recorded.append)
    bus.subscribe(MarketContextUpdated, recorded.append)
    classifier = MarketSessionClassifier(
        schedule=_SCHEDULE, calendar=TradingCalendar(), exchange_timezone=_TZ
    )
    coordinator = HistoricalCoordinator(
        source=FakeHistoricalSource(direct_timeframes=_DIRECT),
        cache=HistoricalCache(),
        max_concurrency=4,
    )
    service = HistoricalWarmupService(
        registry=registry, coordinator=coordinator, planner=_planner(), candles=engine
    )
    tick_engine = TickEngine(
        registry=registry,
        bus=bus,
        clock=ManualClock(_REFERENCE),
        sequence=MonotonicSequence(),
        session=classifier,
        candles=engine,
    )
    return tick_engine, service, engine, registry, recorded


async def test_atomic_surfacing_of_history_and_repairs_in_one_version() -> None:
    tick_engine, service, engine, registry, recorded = _wired()
    instrument = _instrument()
    # Two prior-session incomplete 5m buckets.
    engine.update(_tick(_PRIOR, 9, 16), _session_ctx())
    engine.update(_tick(_PRIOR, 9, 21), _session_ctx())
    engine.flush(datetime.combine(_PRIOR, time(9, 25), tzinfo=_IST).astimezone(UTC))
    await service.warmup(
        [instrument], (HistoricalRequirement(timeframe=_FIVE, lookback=3),), reference=_REFERENCE
    )
    summary = await service.reconcile_completed(instrument, reference=_REFERENCE)
    assert [r.outcome for r in summary.results].count(ReconciliationOutcome.RECONCILED) == 2

    assert recorded == []  # neither warmup nor reconciliation minted a version/event
    today = _REFERENCE.astimezone(_IST).date()
    result = tick_engine.process(_tick(today, 9, 17))
    assert result.context is not None
    assert result.context.version == 1  # exactly one version increment
    assert len(recorded) == 1
    assert result.context.historical is not None
    five = next(c for c in result.context.candle_sets if c.timeframe == _FIVE)
    assert len(five.finalized) == 2  # both repaired candles surfaced together


def _session_ctx() -> SessionContext:
    return SessionContext(
        trading_date=_PRIOR, market_state=MarketState.LIVE_SESSION, exchange_timezone=_TZ
    )


async def _replay_warmup() -> tuple[object, int, list[str]]:
    engine = CandleEngine(schedule=_SCHEDULE, exchange_timezone=_TZ, timeframes=[_FIVE])
    registry = InstrumentStateRegistry([_instrument()])
    coordinator = HistoricalCoordinator(
        source=FakeHistoricalSource(direct_timeframes=_DIRECT),
        cache=HistoricalCache(),
        max_concurrency=4,
    )
    service = HistoricalWarmupService(
        registry=registry, coordinator=coordinator, planner=_planner(), candles=engine
    )
    await service.warmup(
        [_instrument()],
        (
            HistoricalRequirement(timeframe=_FIVE, lookback=10),
            HistoricalRequirement(timeframe=_SEVEN, lookback=5),
        ),
        reference=_REFERENCE,
    )
    state = registry.get(_instrument())
    assert state is not None and state.historical is not None
    labels = [s.timeframe.label for s in state.historical.series]
    return state.historical, len(state.historical.series), labels


async def test_replay_warmup_is_deterministic() -> None:
    first_ctx, first_len, first_labels = await _replay_warmup()
    second_ctx, second_len, second_labels = await _replay_warmup()
    assert first_ctx == second_ctx
    assert (first_len, first_labels) == (second_len, second_labels)


async def _replay_reconciliation() -> list[ReconciliationOutcome]:
    engine = CandleEngine(schedule=_SCHEDULE, exchange_timezone=_TZ, timeframes=[_FIVE])
    engine.update(_tick(_PRIOR, 9, 16), _session_ctx())
    engine.update(_tick(_PRIOR, 9, 21), _session_ctx())
    engine.flush(datetime.combine(_PRIOR, time(9, 25), tzinfo=_IST).astimezone(UTC))
    coordinator = HistoricalCoordinator(
        source=FakeHistoricalSource(direct_timeframes=_DIRECT),
        cache=HistoricalCache(),
        max_concurrency=4,
    )
    service = HistoricalWarmupService(
        registry=InstrumentStateRegistry([_instrument()]),
        coordinator=coordinator,
        planner=_planner(),
        candles=engine,
    )
    summary = await service.reconcile_completed(_instrument(), reference=_REFERENCE)
    return [r.outcome for r in summary.results]


async def test_replay_reconciliation_is_deterministic() -> None:
    assert await _replay_reconciliation() == await _replay_reconciliation()


async def test_registration_order_does_not_change_warmup() -> None:
    async def warm(order: list[HistoricalRequirement]) -> object:
        registry_req = HistoricalRequirementRegistry()
        for index, requirement in enumerate(order):
            registry_req.register(f"c{index}", [requirement])
        engine = CandleEngine(schedule=_SCHEDULE, exchange_timezone=_TZ, timeframes=[_FIVE])
        coordinator = HistoricalCoordinator(
            source=FakeHistoricalSource(direct_timeframes=_DIRECT),
            cache=HistoricalCache(),
            max_concurrency=4,
        )
        state_registry = InstrumentStateRegistry([_instrument()])
        service = HistoricalWarmupService(
            registry=state_registry, coordinator=coordinator, planner=_planner(), candles=engine
        )
        await service.warmup(
            [_instrument()], registry_req.effective_requirements(), reference=_REFERENCE
        )
        installed = state_registry.get(_instrument())
        assert installed is not None
        return installed.historical

    forward = [
        HistoricalRequirement(timeframe=_FIVE, lookback=10),
        HistoricalRequirement(timeframe=_SEVEN, lookback=5),
    ]
    assert await warm(forward) == await warm(list(reversed(forward)))


async def test_old_marketcontext_versions_are_immutable() -> None:
    tick_engine, service, engine, registry, _ = _wired()
    instrument = _instrument()
    today = _REFERENCE.astimezone(_IST).date()
    v1 = tick_engine.process(_tick(today, 9, 16)).context
    assert v1 is not None
    assert v1.historical is None  # no history installed yet at v1

    await service.warmup(
        [instrument], (HistoricalRequirement(timeframe=_FIVE, lookback=2),), reference=_REFERENCE
    )
    v2 = tick_engine.process(_tick(today, 9, 17)).context
    assert v2 is not None
    assert v2.version == 2
    assert v1.version == 1  # untouched
    assert v1.historical is None  # v1 never gained history retroactively
