"""Session-stamping integration in the tick engine (docs/06 §7-§8, §11)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.events.bus import Event, EventBus
from app.market_engine.candle_engine import CandleEngine
from app.market_engine.clock import ManualClock
from app.market_engine.context import MarketState
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.historical.calendar_window import CalendarCoverage
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.session import MarketSessionClassifier, SessionSchedule, TradingCalendar
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.tick_engine import TickEngine
from app.market_engine.timeframe import Timeframe
from app.market_engine.validation import ValidationOutcome
from app.schemas.market_data import Instrument, Tick

_NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
_LIVE = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)  # 12:00 IST, live session
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


def _tick(symbol: str = "RELIANCE", *, at: datetime = _LIVE, price: str = "100") -> Tick:
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=at,
        last_price=Decimal(price),
        traded_quantity=1,
    )


def _classifier(*, holidays: tuple[date, ...] = ()) -> MarketSessionClassifier:
    return MarketSessionClassifier(
        schedule=_SCHEDULE,
        calendar=TradingCalendar(holidays=holidays),
        exchange_timezone="Asia/Kolkata",
    )


def _engine(**kwargs: object) -> tuple[TickEngine, list[Event]]:
    registry = InstrumentStateRegistry(_instrument(symbol) for symbol in _SYMBOLS)
    bus = EventBus()
    recorded: list[Event] = []
    bus.subscribe(MarketContextCreated, recorded.append)
    bus.subscribe(MarketContextUpdated, recorded.append)
    engine = TickEngine(
        registry=registry,
        bus=bus,
        clock=ManualClock(_NOW),
        sequence=MonotonicSequence(),
        session=kwargs.get("session", _classifier()),  # type: ignore[arg-type]
    )
    return engine, recorded


def test_accepted_tick_stamps_session_facts() -> None:
    engine, _ = _engine()
    result = engine.process(_tick())
    assert result.context is not None
    assert result.context.session is not None
    assert result.context.session.market_state is MarketState.LIVE_SESSION
    assert result.context.session.trading_date == date(2026, 8, 6)
    assert result.context.session.exchange_timezone == "Asia/Kolkata"


def test_one_accepted_tick_produces_exactly_one_version() -> None:
    engine, recorded = _engine()
    engine.process(_tick())
    assert [type(event) for event in recorded] == [MarketContextCreated]
    result = engine.process(_tick(at=_LIVE + timedelta(seconds=1), price="101"))
    assert result.context is not None
    assert result.context.version == 2
    assert len(recorded) == 2


def test_session_context_is_immutable() -> None:
    engine, _ = _engine()
    context = engine.process(_tick()).context
    assert context is not None and context.session is not None
    with pytest.raises(ValidationError):
        context.session.market_state = MarketState.HOLIDAY  # type: ignore[misc]


def test_previous_context_session_is_unchanged_after_update() -> None:
    engine, _ = _engine()
    first = engine.process(_tick()).context
    engine.process(_tick(at=_LIVE + timedelta(seconds=1), price="101"))
    assert first is not None and first.session is not None
    assert first.version == 1
    assert first.session.market_state is MarketState.LIVE_SESSION


def test_duplicate_does_not_produce_a_session_only_update() -> None:
    engine, recorded = _engine()
    tick = _tick()
    engine.process(tick)
    result = engine.process(tick)
    assert result.outcome is ValidationOutcome.DUPLICATE
    assert result.context is None
    assert len(recorded) == 1


def test_stale_event_does_not_alter_session_or_context() -> None:
    engine, recorded = _engine()
    engine.process(_tick(at=_LIVE + timedelta(seconds=10)))
    result = engine.process(_tick(at=_LIVE, price="99"))
    assert result.outcome is ValidationOutcome.STALE
    assert result.context is None
    assert len(recorded) == 1


def test_invalid_event_does_not_alter_session_or_context() -> None:
    engine, recorded = _engine()
    result = engine.process(_tick(symbol="UNKNOWN"))
    assert result.outcome is ValidationOutcome.INVALID
    assert result.context is None
    assert recorded == []


def test_halt_fact_is_reflected_on_the_next_accepted_update() -> None:
    engine, _ = _engine()
    engine.process(_tick())
    engine.set_halt(active=True)
    result = engine.process(_tick(at=_LIVE + timedelta(seconds=1), price="101"))
    assert result.context is not None and result.context.session is not None
    assert result.context.session.market_state is MarketState.EMERGENCY_HALT


def test_instrument_isolation_shares_calendar_but_keeps_independent_versions() -> None:
    engine, _ = _engine()
    engine.process(_tick("RELIANCE"))
    engine.process(_tick("RELIANCE", at=_LIVE + timedelta(seconds=1)))
    tcs = engine.process(_tick("TCS"))
    assert tcs.context is not None and tcs.context.session is not None
    assert tcs.context.version == 1
    assert tcs.context.session.market_state is MarketState.LIVE_SESSION


def _stream() -> list[tuple[str, int, str]]:
    engine, recorded = _engine()
    offsets = [("RELIANCE", 0), ("TCS", 0), ("RELIANCE", 1), ("TCS", 1)]
    for symbol, offset in offsets:
        engine.process(_tick(symbol, at=_LIVE + timedelta(seconds=offset), price=f"{100 + offset}"))
    result: list[tuple[str, int, str]] = []
    for event in recorded:
        context = event.context  # type: ignore[attr-defined]
        session = context.session
        assert session is not None
        result.append((context.instrument.symbol, context.version, session.market_state.value))
    return result


def test_session_stamping_is_replay_deterministic() -> None:
    assert _stream() == _stream()


# --------------------------------------------------------------------------- #
# Out-of-coverage fail-closed (ADR-011 live out-of-coverage addendum LC10/LC12)
# --------------------------------------------------------------------------- #
_OOC = datetime(2025, 12, 31, 6, 30, tzinfo=UTC)  # 12:00 IST, before 2026 coverage
_COVERAGE = CalendarCoverage(start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))


def _coverage_engine() -> TickEngine:
    registry = InstrumentStateRegistry(_instrument(symbol) for symbol in _SYMBOLS)
    classifier = MarketSessionClassifier(
        schedule=_SCHEDULE,
        calendar=TradingCalendar(),
        exchange_timezone="Asia/Kolkata",
        coverage=_COVERAGE,
    )
    candles = CandleEngine(
        schedule=_SCHEDULE, exchange_timezone="Asia/Kolkata", timeframes=(Timeframe.minutes(1),)
    )
    return TickEngine(
        registry=registry,
        bus=EventBus(),
        clock=ManualClock(_OOC),  # clock matches the out-of-coverage instant (no future skew)
        sequence=MonotonicSequence(),
        session=classifier,
        candles=candles,
    )


def test_calendar_unavailable_tick_makes_no_live_candle_or_statistics_progression() -> None:
    engine = _coverage_engine()
    result = engine.process(_tick(at=_OOC))
    assert result.context is not None and result.context.session is not None
    assert result.context.session.market_state is MarketState.CALENDAR_UNAVAILABLE
    # Fail-closed: the LIVE_SESSION gates skip, so no candle progresses (no partial and no
    # finalized candles) and no session statistics are established out of coverage.
    assert all(
        candles.partial is None and candles.finalized == ()
        for candles in result.context.candle_sets
    )
    assert result.context.session_statistics is None
