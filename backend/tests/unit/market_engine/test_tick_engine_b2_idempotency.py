"""Authoritative-sink duplicate safety — B2 apply->mark crash-redelivery closure (PHASE H8A).

B2 is the window where a canonical event is applied to authoritative state, the process (or
its durable C1 mark) is lost, and the SAME event is redelivered. ADR-025 froze the resolution
as *value-convergence*: every authoritative market-value mutation is replace / max / min /
delta-of-replaced-snapshot with no local accumulators, and the engine gates an identical
re-delivery so it never mutates state a second time. This module proves that at the authoritative
mutation boundary itself — the ``TickEngine`` and ``CandleEngine`` — with no Redis, no consumer,
no broker.

The proofs, mapped to the H8A matrix:

* T03/T11/T12 — an identical Tick/Quote re-apply is DUPLICATE: no version, no publish, no value
  change (``validation`` value-equality gate + replace-only mutations).
* T13/T14 — candle OHLC (max/min/replace) and interval volume (``last_cumulative - baseline``,
  a delta of replaced snapshots) are convergent even if a duplicate reaches aggregation.
* T15 — the engine holds no per-event trade/tick counter folded into a market value.
* T16 — an identical MarketReference re-apply is DUPLICATE (the H8A "reference gate"); a genuine
  new close still applies.
* T17 — a Tick carrying ``session_ohlc`` is value-equal on re-apply, so the aggregate is gated.
* T18 — instruments are isolated: a duplicate on one never perturbs another.
* T22 — no mark-before-apply loss: a *distinct* event is never suppressed.
* T23/§35 — property: a full replay with every identity redelivered at its recovery position
  yields byte-identical final context (values AND version) to the clean baseline, at 10,000
  events. Any unprotected accumulator would diverge the version or a value and fail this.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from app.events.bus import Event, EventBus
from app.market_engine.candle_engine import CandleEngine
from app.market_engine.clock import ManualClock
from app.market_engine.context import MarketContext, MarketState, SessionContext
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.session import MarketSessionClassifier, SessionSchedule, TradingCalendar
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.tick_engine import TickEngine
from app.market_engine.timeframe import Timeframe
from app.market_engine.validation import ValidationOutcome
from app.schemas.market_data import (
    Instrument,
    MarketReference,
    ProviderSessionOhlc,
    Quote,
    Tick,
)

_NOW = datetime(2026, 9, 14, 7, 0, tzinfo=UTC)
_T0 = datetime(2026, 9, 14, 6, 30, tzinfo=UTC)
_IST = "Asia/Kolkata"
_DATE = date(2026, 9, 14)
_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)
_SYMBOLS = ("RELIANCE", "TCS", "INFY")


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


def _reference(symbol: str = "RELIANCE", *, previous_close: str = "100") -> MarketReference:
    return MarketReference(instrument=_instrument(symbol), previous_close=Decimal(previous_close))


def _tick_with_ohlc(symbol: str = "RELIANCE", *, offset: int = 0) -> Tick:
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=_T0 + timedelta(seconds=offset),
        last_price=Decimal("100.5"),
        traded_quantity=10,
        session_cumulative_volume=1_000,
        session_ohlc=ProviderSessionOhlc(
            open_price=Decimal("99"),
            high_price=Decimal("101"),
            low_price=Decimal("98"),
            close_price=Decimal("100.5"),
        ),
    )


def _classifier() -> MarketSessionClassifier:
    return MarketSessionClassifier(
        schedule=_SCHEDULE, calendar=TradingCalendar(holidays=()), exchange_timezone=_IST
    )


def _engine(*, with_session: bool = False) -> tuple[TickEngine, list[Event]]:
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
        session=_classifier() if with_session else None,
    )
    return engine, recorded


def _snapshot(context: MarketContext | None) -> tuple[object, ...]:
    """Reduce a context to the market VALUES that must survive a duplicate (version excluded)."""
    assert context is not None
    return (
        context.instrument,
        context.latest_tick,
        context.latest_quote,
        context.previous_close,
        context.session_statistics,
        context.candle_sets,
    )


# --------------------------------------------------------------------------- #
# T03 / T11 / T12 — identical Tick / Quote re-apply is DUPLICATE (no second mutation)
# --------------------------------------------------------------------------- #
def test_h8a_t03_t11_identical_tick_reapply_is_duplicate_no_second_mutation() -> None:
    engine, recorded = _engine()
    tick = _tick(offset=0, price="100")
    first = engine.process(tick)
    assert first.outcome is ValidationOutcome.ACCEPT

    replay = engine.process(tick)  # the apply->mark crash redelivery: the exact same event
    assert replay.outcome is ValidationOutcome.DUPLICATE
    assert replay.context is None
    # Exactly one context ever published; the authoritative value is the single application.
    assert [type(e) for e in recorded] == [MarketContextCreated]
    assert first.context is not None and first.context.version == 1
    assert _snapshot(engine.process(_tick(offset=1, price="101")).context)  # engine still advances


def test_h8a_t12_identical_quote_reapply_is_duplicate_no_second_mutation() -> None:
    engine, recorded = _engine()
    quote = _quote(offset=0)
    engine.process(quote)
    replay = engine.process(quote)
    assert replay.outcome is ValidationOutcome.DUPLICATE
    assert replay.context is None
    assert [type(e) for e in recorded] == [MarketContextCreated]


def test_h8a_t03_double_apply_equals_single_apply_state() -> None:
    once, _ = _engine()
    twice, _ = _engine()
    tick = _tick(offset=0, price="100")
    once.process(tick)
    twice.process(tick)
    twice.process(tick)  # duplicate
    single = once._registry.get(_instrument())
    doubled = twice._registry.get(_instrument())
    assert single is not None and doubled is not None
    assert _snapshot(single.context) == _snapshot(doubled.context)
    assert single.context.version == doubled.context.version == 1  # duplicate added no version


# --------------------------------------------------------------------------- #
# T13 / T14 — candle OHLC and interval volume are convergent under a duplicate tick
# --------------------------------------------------------------------------- #
def _candle_engine() -> CandleEngine:
    return CandleEngine(
        schedule=_SCHEDULE, exchange_timezone=_IST, timeframes=[Timeframe.minutes(5)]
    )


def _ist_tick(minute: int, *, price: str, cumulative: int) -> Tick:
    return Tick(
        instrument=_instrument(),
        event_timestamp=datetime(2026, 9, 14, minute // 60 + 9, minute % 60, tzinfo=UTC),
        last_price=Decimal(price),
        traded_quantity=1,
        session_cumulative_volume=cumulative,
    )


def _live_session() -> SessionContext:
    return SessionContext(
        trading_date=_DATE, market_state=MarketState.LIVE_SESSION, exchange_timezone=_IST
    )


def test_h8a_t13_t14_candle_ohlc_and_volume_convergent_under_duplicate() -> None:
    # A clean stream vs. the same stream with a mid-bucket tick and a bucket-boundary tick each
    # applied twice. max/min/close (OHLC) and last_cumulative-baseline (volume) are convergent,
    # so the candle sets must be byte-identical.
    stream = [
        _ist_tick(15, price="100", cumulative=10),
        _ist_tick(16, price="105", cumulative=25),  # high
        _ist_tick(17, price="98", cumulative=40),  # low
        _ist_tick(21, price="102", cumulative=70),  # next 5m bucket (rollover finalizes prior)
    ]
    session = _live_session()

    clean = _candle_engine()
    for tick in stream:
        clean.update(tick, session)

    dup = _candle_engine()
    for i, tick in enumerate(stream):
        dup.update(tick, session)
        if i in (1, 3):  # duplicate a mid-bucket tick and the boundary (rollover) tick
            dup.update(tick, session)

    assert clean.candle_sets_for(_instrument()) == dup.candle_sets_for(_instrument())


# --------------------------------------------------------------------------- #
# T15 — no per-event trade/tick counter is folded into a market value
# --------------------------------------------------------------------------- #
def test_h8a_t15_no_market_value_trade_count_accumulates() -> None:
    once, _ = _engine()
    twice, _ = _engine()
    stream = [_tick(offset=i, price=str(100 + i)) for i in range(5)]
    for tick in stream:
        once.process(tick)
    for tick in stream:
        twice.process(tick)
        twice.process(tick)  # redeliver every one
    a = once._registry.get(_instrument())
    b = twice._registry.get(_instrument())
    assert a is not None and b is not None
    # Same final values and the SAME version count: no counter grew per delivery.
    assert _snapshot(a.context) == _snapshot(b.context)
    assert a.context.version == b.context.version == len(stream)


# --------------------------------------------------------------------------- #
# T16 — the reference gate: identical MarketReference re-apply is DUPLICATE; new close applies
# --------------------------------------------------------------------------- #
def test_h8a_t16_identical_reference_reapply_is_duplicate_gate() -> None:
    engine, recorded = _engine(with_session=True)
    reference = _reference(previous_close="99.25")
    first = engine.process(reference)
    assert first.outcome is ValidationOutcome.ACCEPT
    assert first.context is not None and first.context.previous_close == Decimal("99.25")

    replay = engine.process(reference)  # the previously-ungated apply->mark redelivery
    assert replay.outcome is ValidationOutcome.DUPLICATE
    assert replay.context is None
    assert [type(e) for e in recorded] == [MarketContextCreated]  # no duplicate version bump


def test_h8a_t16_reference_with_a_new_close_still_applies() -> None:
    engine, _ = _engine(with_session=True)
    engine.process(_reference(previous_close="99.25"))
    corrected = engine.process(_reference(previous_close="101.50"))
    assert corrected.outcome is ValidationOutcome.ACCEPT
    assert corrected.context is not None
    assert corrected.context.previous_close == Decimal("101.50")
    assert corrected.context.version == 2


def test_h8a_t16_reference_double_apply_equals_single_apply_state() -> None:
    once, _ = _engine(with_session=True)
    twice, _ = _engine(with_session=True)
    reference = _reference(previous_close="99.25")
    once.process(reference)
    twice.process(reference)
    twice.process(reference)
    a = once._registry.get(_instrument())
    b = twice._registry.get(_instrument())
    assert a is not None and b is not None
    assert a.context.previous_close == b.context.previous_close == Decimal("99.25")
    assert a.context.version == b.context.version == 1


def test_h8a_t16_flat_close_new_session_reference_still_applies() -> None:
    # A new-session reference whose previous_close equals the prior session's (a FLAT close) must
    # NOT be suppressed by the value-equality gate: the gate is session-scoped, so it re-stamps the
    # new session and previous_close survives the rollover carry-forward instead of being cleared.
    registry = InstrumentStateRegistry(_instrument(symbol) for symbol in _SYMBOLS)
    clock = ManualClock(datetime(2026, 9, 14, 7, 0, tzinfo=UTC))  # day D, live session
    engine = TickEngine(
        registry=registry,
        bus=EventBus(),
        clock=clock,
        sequence=MonotonicSequence(),
        session=_classifier(),
    )
    engine.process(_reference(previous_close="100"))  # day D reference, session D
    day_d_tick = Tick(
        instrument=_instrument(),
        event_timestamp=datetime(2026, 9, 14, 6, 30, tzinfo=UTC),
        last_price=Decimal("101"),
        traded_quantity=1,
    )
    engine.process(day_d_tick)  # carries previous_close=100 within session D

    clock.set(datetime(2026, 9, 15, 7, 0, tzinfo=UTC))  # roll to day D+1
    rollover = engine.process(_reference(previous_close="100"))  # flat close: SAME value, new day
    assert rollover.outcome is ValidationOutcome.ACCEPT  # NOT suppressed across the rollover
    assert rollover.context is not None
    assert rollover.context.previous_close == Decimal("100")
    assert rollover.context.session is not None
    assert rollover.context.session.trading_date == date(2026, 9, 15)

    day_d1_tick = Tick(
        instrument=_instrument(),
        event_timestamp=datetime(2026, 9, 15, 6, 30, tzinfo=UTC),
        last_price=Decimal("102"),
        traded_quantity=1,
    )
    result = engine.process(day_d1_tick)
    assert result.context is not None
    assert result.context.previous_close == Decimal("100")  # survived rollover, not cleared to None


# --------------------------------------------------------------------------- #
# T17 — a Tick carrying session_ohlc is value-equal on re-apply, so the aggregate is gated
# --------------------------------------------------------------------------- #
def test_h8a_t17_tick_with_session_ohlc_reapply_is_duplicate() -> None:
    engine, recorded = _engine(with_session=True)
    tick = _tick_with_ohlc(offset=0)
    engine.process(tick)
    replay = engine.process(tick)
    assert replay.outcome is ValidationOutcome.DUPLICATE
    assert replay.context is None
    assert [type(e) for e in recorded] == [MarketContextCreated]


# --------------------------------------------------------------------------- #
# T18 — instruments are isolated: a duplicate on one never perturbs another
# --------------------------------------------------------------------------- #
def test_h8a_t18_duplicate_on_one_instrument_does_not_touch_another() -> None:
    engine, _ = _engine()
    engine.process(_tick("RELIANCE", offset=0, price="100"))
    tcs = engine.process(_tick("TCS", offset=0, price="200"))
    engine.process(_tick("RELIANCE", offset=0, price="100"))  # duplicate on RELIANCE only
    assert tcs.context is not None
    tcs_state = engine._registry.get(_instrument("TCS"))
    assert tcs_state is not None and tcs_state.context is not None
    assert tcs_state.context.version == 1  # untouched by RELIANCE's duplicate


# --------------------------------------------------------------------------- #
# T22 — no mark-before-apply loss: a distinct event is never suppressed
# --------------------------------------------------------------------------- #
def test_h8a_t22_distinct_event_is_never_suppressed() -> None:
    engine, _ = _engine()
    engine.process(_tick(offset=0, price="100"))
    distinct = engine.process(_tick(offset=1, price="101"))  # different value AND time
    assert distinct.outcome is ValidationOutcome.ACCEPT
    assert distinct.context is not None and distinct.context.version == 2


# --------------------------------------------------------------------------- #
# T23 / §35 — property: a full replay with every identity redelivered at its recovery
# position yields identical final context (values AND version) to the clean baseline, at scale.
# --------------------------------------------------------------------------- #
def _replay(engine: TickEngine, *, redeliver: bool, count: int) -> None:
    for i in range(count):
        symbol = _SYMBOLS[i % len(_SYMBOLS)]
        selector = i % 3
        if selector == 0:
            event: Tick | Quote | MarketReference = _tick(symbol, offset=i, price=str(100 + i % 40))
        elif selector == 1:
            event = _quote(symbol, offset=i, bid=str(90 + i % 5), ask=str(110 + i % 5))
        else:
            event = _reference(symbol, previous_close=str(50 + i % 30))
        engine.process(event)
        if redeliver:  # the apply->mark crash window: the exact same event arrives again
            engine.process(event)


def test_h8a_t23_property_10000_event_replay_with_duplicates_matches_clean_baseline() -> None:
    count = 10_000
    clean, _ = _engine(with_session=True)
    _replay(clean, redeliver=False, count=count)
    dirty, _ = _engine(with_session=True)
    _replay(dirty, redeliver=True, count=count)  # every event redelivered once

    for symbol in _SYMBOLS:
        a = clean._registry.get(_instrument(symbol))
        b = dirty._registry.get(_instrument(symbol))
        assert a is not None and b is not None
        assert _snapshot(a.context) == _snapshot(b.context)
        # The strong assertion: duplicates minted NO extra versions anywhere.
        assert a.context.version == b.context.version
