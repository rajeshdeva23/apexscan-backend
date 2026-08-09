"""Candle integration in the accepted-update path (docs/06 §11; §20, §24)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.events.bus import Event, EventBus
from app.market_engine.candle_engine import CandleEngine
from app.market_engine.clock import ManualClock
from app.market_engine.context import CandleQuality, MarketContext, TimeframeCandles
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.session import MarketSessionClassifier, SessionSchedule, TradingCalendar
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.tick_engine import TickEngine
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import (
    FeedContinuity,
    FeedContinuityEvent,
    Instrument,
    Quote,
    Tick,
)

_IST = ZoneInfo("Asia/Kolkata")
_NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
_SCHEDULE = SessionSchedule(
    pre_open_start=datetime(2000, 1, 1, 9, 0).time(),
    opening_auction_start=datetime(2000, 1, 1, 9, 8).time(),
    regular_open=datetime(2000, 1, 1, 9, 15).time(),
    regular_close=datetime(2000, 1, 1, 15, 30).time(),
    closing_end=datetime(2000, 1, 1, 15, 40).time(),
)
_TIMEFRAMES = [Timeframe.minutes(1), Timeframe.minutes(5)]


def _instrument() -> Instrument:
    return Instrument(exchange="NSE", symbol="RELIANCE")


def _tick(at: datetime, *, price: str = "100", cumulative: int | None = None) -> Tick:
    return Tick(
        instrument=_instrument(),
        event_timestamp=at,
        last_price=Decimal(price),
        traded_quantity=1,
        session_cumulative_volume=cumulative,
    )


def _quote(at: datetime) -> Quote:
    return Quote(
        instrument=_instrument(),
        event_timestamp=at,
        bid_price=Decimal("99"),
        ask_price=Decimal("101"),
        bid_quantity=1,
        ask_quantity=1,
    )


def _engine() -> tuple[TickEngine, list[Event]]:
    registry = InstrumentStateRegistry([_instrument()])
    bus = EventBus()
    recorded: list[Event] = []
    bus.subscribe(MarketContextCreated, recorded.append)
    bus.subscribe(MarketContextUpdated, recorded.append)
    classifier = MarketSessionClassifier(
        schedule=_SCHEDULE, calendar=TradingCalendar(), exchange_timezone="Asia/Kolkata"
    )
    candles = CandleEngine(
        schedule=_SCHEDULE, exchange_timezone="Asia/Kolkata", timeframes=_TIMEFRAMES
    )
    engine = TickEngine(
        registry=registry,
        bus=bus,
        clock=ManualClock(_NOW),
        sequence=MonotonicSequence(),
        session=classifier,
        candles=candles,
    )
    return engine, recorded


def _sets(context: MarketContext) -> dict[Timeframe, TimeframeCandles]:
    return {candles.timeframe: candles for candles in context.candle_sets}


def test_accepted_tick_stamps_candle_sets_for_registered_timeframes() -> None:
    engine, _ = _engine()
    result = engine.process(_tick(datetime(2026, 8, 6, 9, 16, tzinfo=_IST), cumulative=1000))
    assert result.context is not None
    sets = _sets(result.context)
    assert set(sets) == {Timeframe.minutes(1), Timeframe.minutes(5)}
    assert sets[Timeframe.minutes(5)].partial is not None


def test_one_tick_updating_multiple_timeframes_is_one_version() -> None:
    engine, recorded = _engine()
    result = engine.process(_tick(datetime(2026, 8, 6, 9, 16, tzinfo=_IST), cumulative=1000))
    assert result.context is not None
    assert result.context.version == 1
    assert len(recorded) == 1  # a single MarketContextCreated, not one per timeframe


def test_quote_does_not_advance_candles_but_context_carries_them() -> None:
    engine, _ = _engine()
    engine.process(_tick(datetime(2026, 8, 6, 9, 16, tzinfo=_IST), price="100", cumulative=1000))
    result = engine.process(_quote(datetime(2026, 8, 6, 9, 21, tzinfo=_IST)))
    assert result.context is not None
    five = _sets(result.context)[Timeframe.minutes(5)].partial
    assert five is not None
    # The quote did not open a new (09:20) bucket; candles still reflect the 09:16 tick.
    assert five.start_timestamp == datetime(2026, 8, 6, 3, 45, tzinfo=UTC)
    assert five.close_price == Decimal("100")


def test_candle_sets_are_immutable_in_the_context() -> None:
    engine, _ = _engine()
    context = engine.process(
        _tick(datetime(2026, 8, 6, 9, 16, tzinfo=_IST), cumulative=1000)
    ).context
    assert context is not None
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        context.candle_sets = ()  # type: ignore[misc]


def _replay() -> list[tuple[int, str]]:
    engine, recorded = _engine()
    for offset, cum in ((0, 1000), (1, 1030), (5, 1070)):
        at = datetime(2026, 8, 6, 9, 16, tzinfo=_IST) + timedelta(minutes=offset)
        engine.process(_tick(at, price=f"{100 + offset}", cumulative=cum))
    log: list[tuple[int, str]] = []
    for event in recorded:
        context = event.context  # type: ignore[attr-defined]
        five = _sets(context)[Timeframe.minutes(5)].partial
        assert five is not None
        log.append((context.version, str(five.close_price)))
    return log


def test_candle_integration_is_replay_deterministic() -> None:
    assert _replay() == _replay()


def test_continuity_fact_does_not_mint_an_event_less_context_version() -> None:
    engine, recorded = _engine()
    engine.process(_tick(datetime(2026, 8, 6, 9, 16, tzinfo=_IST), cumulative=1000))
    assert len(recorded) == 1
    engine.on_feed_continuity(
        FeedContinuityEvent(status=FeedContinuity.CONTINUITY_LOST, observed_at=_NOW)
    )
    assert len(recorded) == 1  # no event-less MarketContext version minted (ADR-006 §28)
    result = engine.process(_tick(datetime(2026, 8, 6, 9, 17, tzinfo=_IST), cumulative=1005))
    assert result.context is not None
    assert result.context.version == 2
    five = _sets(result.context)[Timeframe.minutes(5)].partial
    assert five is not None
    assert five.quality is not CandleQuality.COMPLETE  # taint surfaces on the next accepted tick


def test_context_separates_authoritative_and_incomplete_candles() -> None:
    engine, _ = _engine()
    engine.process(_tick(datetime(2026, 8, 6, 9, 16, tzinfo=_IST), cumulative=1000))  # b0
    result = engine.process(_tick(datetime(2026, 8, 6, 9, 21, tzinfo=_IST), cumulative=1070))  # b1
    assert result.context is not None
    five = _sets(result.context)[Timeframe.minutes(5)]
    assert five.finalized == ()  # no authoritative live candle
    assert len(five.incomplete) >= 1  # finalized-incomplete b0 retained, kept separate
