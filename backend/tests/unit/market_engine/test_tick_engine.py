"""Tests for deterministic tick/quote routing and context progression (docs/06 §11-§12)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.events.bus import Event, EventBus
from app.market_engine.clock import ManualClock
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.tick_engine import TickEngine
from app.market_engine.validation import ValidationOutcome
from app.schemas.market_data import Instrument, Quote, Tick

_NOW = datetime(2026, 8, 6, 7, 0, tzinfo=UTC)
_T0 = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _tick(symbol: str = "RELIANCE", *, offset: int = 0, price: str = "100") -> Tick:
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=_T0 + timedelta(seconds=offset),
        last_price=Decimal(price),
        traded_quantity=5,
    )


def _quote(
    symbol: str = "RELIANCE", *, offset: int = 0, bid: str = "99", ask: str = "101"
) -> Quote:
    return Quote(
        instrument=_instrument(symbol),
        event_timestamp=_T0 + timedelta(seconds=offset),
        bid_price=Decimal(bid),
        ask_price=Decimal(ask),
        bid_quantity=1,
        ask_quantity=1,
    )


def _engine(symbols: tuple[str, ...] = ("RELIANCE", "TCS")) -> tuple[TickEngine, list[Event]]:
    registry = InstrumentStateRegistry(_instrument(symbol) for symbol in symbols)
    bus = EventBus()
    recorded: list[Event] = []
    bus.subscribe(MarketContextCreated, recorded.append)
    bus.subscribe(MarketContextUpdated, recorded.append)
    engine = TickEngine(
        registry=registry, bus=bus, clock=ManualClock(_NOW), sequence=MonotonicSequence()
    )
    return engine, recorded


def test_first_tick_creates_version_one_and_publishes_created() -> None:
    engine, recorded = _engine()
    result = engine.process(_tick())
    assert result.outcome is ValidationOutcome.ACCEPT
    assert result.context is not None
    assert result.context.version == 1
    assert [type(event) for event in recorded] == [MarketContextCreated]
    assert recorded[0].context is result.context  # type: ignore[attr-defined]


def test_second_tick_increments_version_and_publishes_updated() -> None:
    engine, recorded = _engine()
    engine.process(_tick(offset=0))
    result = engine.process(_tick(offset=1, price="101"))
    assert result.context is not None
    assert result.context.version == 2
    assert [type(event) for event in recorded] == [MarketContextCreated, MarketContextUpdated]
    assert recorded[1].previous_version == 1  # type: ignore[attr-defined]


def test_quote_after_tick_preserves_the_tick() -> None:
    engine, _ = _engine()
    tick = _tick(offset=0)
    engine.process(tick)
    result = engine.process(_quote(offset=1))
    assert result.context is not None
    assert result.context.latest_tick == tick
    assert result.context.latest_quote is not None


def test_tick_after_quote_preserves_the_quote() -> None:
    engine, _ = _engine()
    quote = _quote(offset=0)
    engine.process(quote)
    result = engine.process(_tick(offset=1))
    assert result.context is not None
    assert result.context.latest_quote == quote
    assert result.context.latest_tick is not None


def test_previous_context_is_not_mutated_by_an_update() -> None:
    engine, _ = _engine()
    first = engine.process(_tick(offset=0)).context
    engine.process(_tick(offset=1, price="102"))
    assert first is not None
    assert first.version == 1
    assert str(first.latest_tick.last_price) == "100"  # type: ignore[union-attr]


def test_sequence_advances_only_on_accepted_events() -> None:
    engine, _ = _engine()
    v1 = engine.process(_tick(offset=0)).context
    v2 = engine.process(_tick(offset=1)).context
    assert v1 is not None
    assert v2 is not None
    assert (v1.sequence, v2.sequence) == (1, 2)


def test_duplicate_is_ignored_with_no_version_or_event() -> None:
    engine, recorded = _engine()
    tick = _tick(offset=0)
    engine.process(tick)
    result = engine.process(tick)
    assert result.outcome is ValidationOutcome.DUPLICATE
    assert result.context is None
    assert [type(event) for event in recorded] == [MarketContextCreated]


def test_stale_event_is_ignored_and_does_not_overwrite_state() -> None:
    engine, recorded = _engine()
    engine.process(_tick(offset=5, price="105"))
    result = engine.process(_tick(offset=1, price="101"))
    assert result.outcome is ValidationOutcome.STALE
    assert result.context is None
    assert [type(event) for event in recorded] == [MarketContextCreated]


def test_out_of_order_event_never_moves_context_time_backwards() -> None:
    engine, _ = _engine()
    engine.process(_tick(offset=5))
    engine.process(_tick(offset=2))  # out-of-order, rejected as stale
    accepted = engine.process(_tick(offset=6, price="106"))
    assert accepted.context is not None
    assert accepted.context.version == 2  # only offsets 5 and 6 were accepted


def test_invalid_event_produces_no_state_change_or_event() -> None:
    engine, recorded = _engine(symbols=("RELIANCE",))
    result = engine.process(_tick(symbol="UNKNOWN"))
    assert result.outcome is ValidationOutcome.INVALID
    assert result.context is None
    assert recorded == []


def test_instrument_isolation_versions_are_independent() -> None:
    engine, _ = _engine()
    engine.process(_tick("RELIANCE", offset=0))
    engine.process(_tick("RELIANCE", offset=1))
    b_result = engine.process(_tick("TCS", offset=0))
    a_result = engine.process(_tick("RELIANCE", offset=2))
    assert b_result.context is not None
    assert a_result.context is not None
    assert b_result.context.version == 1
    assert a_result.context.version == 3


def test_published_event_carries_the_exact_new_context() -> None:
    engine, recorded = _engine()
    result = engine.process(_tick())
    assert recorded[0].context is result.context  # type: ignore[attr-defined]
