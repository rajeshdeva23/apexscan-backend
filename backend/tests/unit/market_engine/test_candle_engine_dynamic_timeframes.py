"""Dynamic timeframe activation/removal on the live candle engine (P5.4; ADR-007 D9)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.market_engine.candle_engine import CandleEngine
from app.market_engine.context import MarketState, SessionContext, TimeframeCandles
from app.market_engine.session import SessionSchedule
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument, Tick

_IST = ZoneInfo("Asia/Kolkata")
_DATE = date(2026, 8, 6)
_SCHEDULE = SessionSchedule(
    pre_open_start=datetime(2000, 1, 1, 9, 0).time(),
    opening_auction_start=datetime(2000, 1, 1, 9, 8).time(),
    regular_open=datetime(2000, 1, 1, 9, 15).time(),
    regular_close=datetime(2000, 1, 1, 15, 30).time(),
    closing_end=datetime(2000, 1, 1, 15, 40).time(),
)
_M5 = Timeframe.minutes(5)
_M15 = Timeframe.minutes(15)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _session() -> SessionContext:
    return SessionContext(
        trading_date=_DATE, market_state=MarketState.LIVE_SESSION, exchange_timezone="Asia/Kolkata"
    )


def _tick(symbol: str, hour: int, minute: int, *, price: str) -> Tick:
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=datetime(2026, 8, 6, hour, minute, tzinfo=_IST),
        last_price=Decimal(price),
    )


def _engine(*timeframes: Timeframe) -> CandleEngine:
    return CandleEngine(
        schedule=_SCHEDULE, exchange_timezone="Asia/Kolkata", timeframes=list(timeframes)
    )


def _set(engine: CandleEngine, symbol: str, timeframe: Timeframe) -> TimeframeCandles | None:
    sets = {c.timeframe: c for c in engine.candle_sets_for(_instrument(symbol))}
    return sets.get(timeframe)


# --------------------------------------------------------------------------- #
# Adding a timeframe (§10)
# --------------------------------------------------------------------------- #
def test_added_timeframe_appears_in_the_active_set() -> None:
    engine = _engine(_M5)
    engine.set_required_timeframes(frozenset({_M5, _M15}))
    assert set(engine.timeframes) == {_M5, _M15}


def test_adding_a_timeframe_preserves_existing_timeframe_state() -> None:
    engine = _engine(_M5)
    engine.update(_tick("RELIANCE", 9, 16, price="100"), _session())  # opens 5m bucket at 100
    engine.set_required_timeframes(frozenset({_M5, _M15}))
    engine.update(_tick("RELIANCE", 9, 17, price="101"), _session())

    five = _set(engine, "RELIANCE", _M5)
    assert five is not None and five.partial is not None
    assert five.partial.open_price == Decimal("100")  # unchanged across the add
    assert five.partial.high_price == Decimal("101")


def test_newly_added_timeframe_starts_from_the_first_post_activation_datum() -> None:
    engine = _engine(_M5)
    engine.update(_tick("RELIANCE", 9, 16, price="100"), _session())  # before 15m activation
    engine.set_required_timeframes(frozenset({_M5, _M15}))
    engine.update(_tick("RELIANCE", 9, 17, price="101"), _session())  # first 15m datum

    fifteen = _set(engine, "RELIANCE", _M15)
    assert fifteen is not None and fifteen.partial is not None
    # 15m opens at 101 (the first observed datum after activation), not the pre-add 100.
    assert fifteen.partial.open_price == Decimal("101")


def test_no_live_candle_is_fabricated_for_the_pre_activation_interval() -> None:
    engine = _engine(_M5)
    engine.update(_tick("RELIANCE", 9, 16, price="100"), _session())
    engine.set_required_timeframes(frozenset({_M5, _M15}))
    engine.update(_tick("RELIANCE", 9, 17, price="101"), _session())

    fifteen = _set(engine, "RELIANCE", _M15)
    assert fifteen is not None
    assert fifteen.finalized == ()  # no fabricated earlier 15m candle
    assert fifteen.incomplete == ()


# --------------------------------------------------------------------------- #
# Removing a timeframe (§11)
# --------------------------------------------------------------------------- #
def test_removed_timeframe_leaves_the_active_set_and_stops_surfacing() -> None:
    engine = _engine(_M5, _M15)
    engine.update(_tick("RELIANCE", 9, 16, price="100"), _session())
    engine.set_required_timeframes(frozenset({_M5}))
    assert set(engine.timeframes) == {_M5}
    assert _set(engine, "RELIANCE", _M15) is None


def test_removing_one_timeframe_preserves_the_retained_timeframe_state() -> None:
    engine = _engine(_M5, _M15)
    engine.update(_tick("RELIANCE", 9, 16, price="100"), _session())
    engine.set_required_timeframes(frozenset({_M5}))

    five = _set(engine, "RELIANCE", _M5)
    assert five is not None and five.partial is not None
    assert five.partial.open_price == Decimal("100")


def test_removed_timeframe_stops_future_aggregation() -> None:
    engine = _engine(_M5, _M15)
    engine.update(_tick("RELIANCE", 9, 16, price="100"), _session())
    engine.set_required_timeframes(frozenset({_M5}))
    engine.update(_tick("RELIANCE", 9, 17, price="101"), _session())  # no 15m should build
    assert _set(engine, "RELIANCE", _M15) is None


# --------------------------------------------------------------------------- #
# Idempotent reconfiguration (§12)
# --------------------------------------------------------------------------- #
def test_reapplying_the_same_set_does_not_reset_state() -> None:
    engine = _engine(_M5, _M15)
    engine.update(_tick("RELIANCE", 9, 16, price="100"), _session())
    engine.update(_tick("RELIANCE", 9, 17, price="103"), _session())

    engine.set_required_timeframes(frozenset({_M5, _M15}))
    engine.set_required_timeframes(frozenset({_M5, _M15}))

    five = _set(engine, "RELIANCE", _M5)
    assert five is not None and five.partial is not None
    assert five.partial.open_price == Decimal("100")  # not reset
    assert five.partial.high_price == Decimal("103")
    assert engine.timeframes.count(_M5) == 1  # no duplicate engine


# --------------------------------------------------------------------------- #
# Remove then re-add starts fresh (§13)
# --------------------------------------------------------------------------- #
def test_remove_then_readd_starts_fresh_live_state() -> None:
    engine = _engine(_M5)
    engine.update(_tick("RELIANCE", 9, 16, price="100"), _session())
    engine.set_required_timeframes(frozenset())  # remove 5m — drop live state
    engine.set_required_timeframes(frozenset({_M5}))  # re-add
    engine.update(_tick("RELIANCE", 9, 18, price="105"), _session())

    five = _set(engine, "RELIANCE", _M5)
    assert five is not None and five.partial is not None
    assert five.partial.open_price == Decimal("105")  # fresh, not resurrected 100


# --------------------------------------------------------------------------- #
# Multi-instrument safety (§14)
# --------------------------------------------------------------------------- #
def test_dynamic_change_does_not_corrupt_other_instrument_state() -> None:
    engine = _engine(_M5)
    engine.update(_tick("RELIANCE", 9, 16, price="100"), _session())
    engine.update(_tick("INFY", 9, 16, price="200"), _session())

    engine.set_required_timeframes(frozenset({_M5, _M15}))
    engine.update(_tick("RELIANCE", 9, 17, price="101"), _session())
    engine.set_required_timeframes(frozenset({_M5}))  # remove 15m again

    reliance = _set(engine, "RELIANCE", _M5)
    infy = _set(engine, "INFY", _M5)
    assert reliance is not None and reliance.partial is not None
    assert reliance.partial.open_price == Decimal("100")
    assert infy is not None and infy.partial is not None
    assert infy.partial.open_price == Decimal("200")  # untouched throughout


# --------------------------------------------------------------------------- #
# Validation carries over to the dynamic seam
# --------------------------------------------------------------------------- #
def test_intraday_timeframe_not_shorter_than_session_is_rejected() -> None:
    engine = _engine(_M5)
    with pytest.raises(ValueError, match="shorter than the session"):
        engine.set_required_timeframes(frozenset({Timeframe.minutes(600)}))


def test_session_timeframe_is_accepted_by_the_dynamic_seam() -> None:
    engine = _engine(_M5)
    engine.set_required_timeframes(frozenset({_M5, Timeframe.session()}))
    assert Timeframe.session() in engine.timeframes
