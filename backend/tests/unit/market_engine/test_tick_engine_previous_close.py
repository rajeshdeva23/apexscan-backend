"""Canonical previous-close (MarketReference) routing and carry-forward (SECTOR-VIEW-1A).

A MarketReference carries a provider-independent prior-session close. It has no wire
timestamp, so the engine stamps its own clock time and classifies the session from it.
The reference surfaces on ``MarketContext.previous_close`` and is preserved across ticks
and quotes that omit it, but reset on a genuine trading-date rollover.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from app.events.bus import Event, EventBus
from app.market_engine.clock import ManualClock
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.session import MarketSessionClassifier, SessionSchedule, TradingCalendar
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.tick_engine import TickEngine
from app.market_engine.validation import ValidationOutcome
from app.schemas.market_data import Instrument, MarketReference, Quote, Tick

_DAY1_CLOCK = datetime(2026, 8, 6, 7, 0, tzinfo=UTC)  # 12:30 IST, live session
_DAY1_LIVE = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)  # 12:00 IST, live session
_DAY2_LIVE = datetime(2026, 8, 7, 6, 30, tzinfo=UTC)  # next trading day, live session
_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)
_SYMBOLS = ("RELIANCE", "TCS")


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _reference(symbol: str = "RELIANCE", *, previous_close: str = "100") -> MarketReference:
    return MarketReference(instrument=_instrument(symbol), previous_close=Decimal(previous_close))


def _tick(symbol: str = "RELIANCE", *, at: datetime = _DAY1_LIVE, price: str = "105") -> Tick:
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=at,
        last_price=Decimal(price),
        traded_quantity=1,
    )


def _quote(symbol: str = "RELIANCE", *, at: datetime = _DAY1_LIVE) -> Quote:
    return Quote(
        instrument=_instrument(symbol),
        event_timestamp=at,
        bid_price=Decimal("104"),
        ask_price=Decimal("106"),
        bid_quantity=1,
        ask_quantity=1,
    )


def _classifier() -> MarketSessionClassifier:
    return MarketSessionClassifier(
        schedule=_SCHEDULE,
        calendar=TradingCalendar(holidays=()),
        exchange_timezone="Asia/Kolkata",
    )


def _engine(clock: ManualClock | None = None) -> tuple[TickEngine, ManualClock, list[Event]]:
    registry = InstrumentStateRegistry(_instrument(symbol) for symbol in _SYMBOLS)
    bus = EventBus()
    recorded: list[Event] = []
    bus.subscribe(MarketContextCreated, recorded.append)
    bus.subscribe(MarketContextUpdated, recorded.append)
    manual = clock or ManualClock(_DAY1_CLOCK)
    engine = TickEngine(
        registry=registry,
        bus=bus,
        clock=manual,
        sequence=MonotonicSequence(),
        session=_classifier(),
    )
    return engine, manual, recorded


def test_reference_first_creates_context_with_previous_close() -> None:
    engine, _, recorded = _engine()
    result = engine.process(_reference(previous_close="100"))
    assert result.outcome is ValidationOutcome.ACCEPT
    assert result.context is not None
    assert result.context.version == 1
    assert result.context.previous_close == Decimal("100")
    assert [type(event) for event in recorded] == [MarketContextCreated]


def test_previous_close_is_none_until_a_reference_arrives() -> None:
    engine, _, _ = _engine()
    result = engine.process(_tick())
    assert result.context is not None
    assert result.context.previous_close is None


def test_reference_after_tick_preserves_the_tick_and_sets_previous_close() -> None:
    engine, _, _ = _engine()
    tick = _tick()
    engine.process(tick)
    result = engine.process(_reference(previous_close="100"))
    assert result.context is not None
    assert result.context.version == 2
    assert result.context.latest_tick == tick
    assert result.context.previous_close == Decimal("100")


def test_previous_close_preserved_across_ticks_that_omit_it() -> None:
    engine, _, _ = _engine()
    engine.process(_reference(previous_close="100"))
    first = engine.process(_tick(at=_DAY1_LIVE, price="105"))
    second = engine.process(_tick(at=_DAY1_LIVE + timedelta(seconds=1), price="106"))
    assert first.context is not None and first.context.previous_close == Decimal("100")
    assert second.context is not None and second.context.previous_close == Decimal("100")


def test_previous_close_preserved_across_a_quote() -> None:
    engine, _, _ = _engine()
    engine.process(_reference(previous_close="100"))
    result = engine.process(_quote())
    assert result.context is not None
    assert result.context.previous_close == Decimal("100")


def test_later_reference_replaces_the_previous_close() -> None:
    engine, _, _ = _engine()
    engine.process(_reference(previous_close="100"))
    result = engine.process(_reference(previous_close="111"))
    assert result.context is not None
    assert result.context.previous_close == Decimal("111")


def test_unknown_instrument_reference_is_rejected_without_mutation() -> None:
    engine, _, recorded = _engine()
    result = engine.process(_reference("UNKNOWN", previous_close="100"))
    assert result.outcome is ValidationOutcome.INVALID
    assert result.context is None
    assert recorded == []


def test_reference_for_one_instrument_does_not_touch_another() -> None:
    engine, _, _ = _engine()
    engine.process(_reference("RELIANCE", previous_close="100"))
    result = engine.process(_tick("TCS"))
    assert result.context is not None
    assert result.context.instrument.symbol == "TCS"
    assert result.context.previous_close is None


def test_trading_date_rollover_resets_previous_close() -> None:
    clock = ManualClock(_DAY1_CLOCK)
    engine, _, _ = _engine(clock)
    day1 = engine.process(_reference(previous_close="100"))
    assert day1.context is not None
    assert day1.context.session is not None
    assert day1.context.session.trading_date == date(2026, 8, 6)

    clock.set(datetime(2026, 8, 7, 7, 0, tzinfo=UTC))
    result = engine.process(_tick(at=_DAY2_LIVE, price="106"))
    assert result.context is not None
    assert result.context.session is not None
    assert result.context.session.trading_date == date(2026, 8, 7)
    assert result.context.previous_close is None


def test_new_day_reference_supplies_the_new_previous_close_after_rollover() -> None:
    clock = ManualClock(_DAY1_CLOCK)
    engine, _, _ = _engine(clock)
    engine.process(_reference(previous_close="100"))
    clock.set(datetime(2026, 8, 7, 7, 0, tzinfo=UTC))
    engine.process(_tick(at=_DAY2_LIVE, price="106"))
    result = engine.process(_reference(previous_close="106"))
    assert result.context is not None
    assert result.context.previous_close == Decimal("106")
