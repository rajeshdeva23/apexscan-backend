"""Tests for the generic, timeframe-agnostic live candle engine (docs/06 §13; ADR-005)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.market_engine.candle_engine import CandleEngine
from app.market_engine.context import (
    CandleQuality,
    IncompleteCandle,
    MarketState,
    SessionContext,
    TimeframeCandles,
)
from app.market_engine.session import SessionSchedule
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, Instrument, Tick
from tests.architecture.import_boundary import forbidden_imports

_IST = ZoneInfo("Asia/Kolkata")
_DATE = date(2026, 8, 6)
_SCHEDULE = SessionSchedule(
    pre_open_start=datetime(2000, 1, 1, 9, 0).time(),
    opening_auction_start=datetime(2000, 1, 1, 9, 8).time(),
    regular_open=datetime(2000, 1, 1, 9, 15).time(),
    regular_close=datetime(2000, 1, 1, 15, 30).time(),
    closing_end=datetime(2000, 1, 1, 15, 40).time(),
)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _session(
    *, trading_date: date = _DATE, state: MarketState = MarketState.LIVE_SESSION
) -> SessionContext:
    return SessionContext(
        trading_date=trading_date, market_state=state, exchange_timezone="Asia/Kolkata"
    )


def _ist(hour: int, minute: int, second: int = 0, micro: int = 0) -> datetime:
    return datetime(2026, 8, 6, hour, minute, second, micro, tzinfo=_IST)


def _utc(hour: int, minute: int) -> datetime:
    return datetime(2026, 8, 6, hour, minute, tzinfo=UTC)


def _tick(
    symbol: str,
    at: datetime,
    *,
    price: str = "100",
    ltq: int = 1,
    cumulative: int | None = None,
) -> Tick:
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=at,
        last_price=Decimal(price),
        traded_quantity=ltq,
        session_cumulative_volume=cumulative,
    )


def _engine(timeframes: list[Timeframe], *, window: int = 20) -> CandleEngine:
    return CandleEngine(
        schedule=_SCHEDULE,
        exchange_timezone="Asia/Kolkata",
        timeframes=timeframes,
        finalized_window=window,
    )


def _set(engine: CandleEngine, symbol: str, timeframe: Timeframe) -> TimeframeCandles:
    sets = {candles.timeframe: candles for candles in engine.candle_sets_for(_instrument(symbol))}
    return sets[timeframe]


# --------------------------------------------------------------------------- #
# No-strategy / no-provider imports (extensibility acceptance §36)
# --------------------------------------------------------------------------- #
def test_candle_engine_module_has_no_strategy_or_provider_imports() -> None:
    import app.market_engine.candle_engine as candle_module

    text = Path(candle_module.__file__).read_text(encoding="utf-8")
    assert forbidden_imports(text, package="app.market_engine") == []


# --------------------------------------------------------------------------- #
# Timeframe registration
# --------------------------------------------------------------------------- #
def test_only_registered_timeframes_are_built() -> None:
    engine = _engine([Timeframe.minutes(5), Timeframe.minutes(15)])
    engine.update(_tick("RELIANCE", _ist(9, 16), cumulative=100), _session())
    timeframes = {candles.timeframe for candles in engine.candle_sets_for(_instrument())}
    assert timeframes == {Timeframe.minutes(5), Timeframe.minutes(15)}


def test_duplicate_registration_is_deduplicated() -> None:
    engine = _engine([Timeframe.minutes(5), Timeframe.minutes(5)])
    assert engine.timeframes == (Timeframe.minutes(5),)


def test_one_tick_updates_every_registered_timeframe() -> None:
    engine = _engine([Timeframe.minutes(1), Timeframe.minutes(5), Timeframe.minutes(15)])
    engine.update(_tick("RELIANCE", _ist(9, 16), cumulative=100), _session())
    for timeframe in (Timeframe.minutes(1), Timeframe.minutes(5), Timeframe.minutes(15)):
        assert _set(engine, "RELIANCE", timeframe).partial is not None


# --------------------------------------------------------------------------- #
# Session-relative alignment (§29) — including an arbitrary 7m (extensibility)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("minutes", "at", "expected_start", "expected_end"),
    [
        (5, _ist(9, 15), _utc(3, 45), _utc(3, 50)),
        (5, _ist(9, 24), _utc(3, 50), _utc(3, 55)),
        (15, _ist(9, 15), _utc(3, 45), _utc(4, 0)),
        (15, _ist(9, 44), _utc(4, 0), _utc(4, 15)),
        (7, _ist(9, 15), _utc(3, 45), _utc(3, 52)),
        (7, _ist(9, 22), _utc(3, 52), _utc(3, 59)),
    ],
)
def test_intraday_buckets_are_anchored_to_regular_open(
    minutes: int, at: datetime, expected_start: datetime, expected_end: datetime
) -> None:
    engine = _engine([Timeframe.minutes(minutes)])
    engine.update(_tick("RELIANCE", at, cumulative=100), _session())
    partial = _set(engine, "RELIANCE", Timeframe.minutes(minutes)).partial
    assert partial is not None
    assert partial.start_timestamp == expected_start
    assert partial.end_timestamp == expected_end


def test_final_bucket_truncates_at_regular_close() -> None:
    # 7m does not divide the 375-minute session; the last bucket ends at 15:30 IST.
    engine = _engine([Timeframe.minutes(7)])
    engine.update(_tick("RELIANCE", _ist(15, 29), cumulative=100), _session())
    partial = _set(engine, "RELIANCE", Timeframe.minutes(7)).partial
    assert partial is not None
    assert partial.end_timestamp == _utc(10, 0)  # 15:30 IST


def test_exact_bucket_boundary_belongs_to_the_next_bucket() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick("RELIANCE", _ist(9, 19, 59, 999999), cumulative=10), _session())
    engine.update(_tick("RELIANCE", _ist(9, 20, 0, 0), cumulative=20), _session())
    partial = _set(engine, "RELIANCE", Timeframe.minutes(5)).partial
    assert partial is not None
    assert partial.start_timestamp == _utc(3, 50)  # 09:20 IST -> second bucket


# --------------------------------------------------------------------------- #
# OHLC (§30)
# --------------------------------------------------------------------------- #
def test_first_tick_sets_open_high_low_close() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick("RELIANCE", _ist(9, 16), price="100", cumulative=10), _session())
    partial = _set(engine, "RELIANCE", Timeframe.minutes(5)).partial
    assert partial is not None
    assert (partial.open_price, partial.high_price, partial.low_price, partial.close_price) == (
        Decimal("100"),
        Decimal("100"),
        Decimal("100"),
        Decimal("100"),
    )


def test_ohlc_updates_but_open_is_fixed() -> None:
    engine = _engine([Timeframe.minutes(5)])
    for price, cum in (("100", 10), ("103", 12), ("98", 15), ("101", 20)):
        engine.update(_tick("RELIANCE", _ist(9, 16), price=price, cumulative=cum), _session())
    partial = _set(engine, "RELIANCE", Timeframe.minutes(5)).partial
    assert partial is not None
    assert partial.open_price == Decimal("100")
    assert partial.high_price == Decimal("103")
    assert partial.low_price == Decimal("98")
    assert partial.close_price == Decimal("101")


# --------------------------------------------------------------------------- #
# Volume (§31) — ADR-006 complete-or-withhold; live intervals are never authoritative
# --------------------------------------------------------------------------- #
def _authoritative(engine: CandleEngine, symbol: str, timeframe: Timeframe) -> tuple[Candle, ...]:
    return _set(engine, symbol, timeframe).finalized


def _incomplete(
    engine: CandleEngine, symbol: str, timeframe: Timeframe
) -> tuple[IncompleteCandle, ...]:
    return _set(engine, symbol, timeframe).incomplete


def test_no_authoritative_candle_is_emitted_from_live_snapshots() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick("RELIANCE", _ist(9, 16), cumulative=1000), _session())
    engine.update(_tick("RELIANCE", _ist(9, 21), cumulative=1070), _session())
    engine.flush(_utc(3, 55))
    assert _authoritative(engine, "RELIANCE", Timeframe.minutes(5)) == ()


def test_first_observed_bucket_is_incomplete_with_no_volume() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick("RELIANCE", _ist(9, 16), cumulative=1000), _session())
    engine.flush(_utc(3, 50))
    incomplete = _incomplete(engine, "RELIANCE", Timeframe.minutes(5))
    assert len(incomplete) == 1
    assert incomplete[0].traded_quantity is None  # no authoritative baseline
    assert incomplete[0].quality is CandleQuality.INCOMPLETE_VOLUME


def test_contiguous_bucket_carries_provisional_boundary_delta() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick("RELIANCE", _ist(9, 16), cumulative=1000), _session())  # bucket 1
    engine.update(_tick("RELIANCE", _ist(9, 21), cumulative=1070), _session())  # bucket 2 opens
    engine.flush(_utc(3, 55))
    second = _incomplete(engine, "RELIANCE", Timeframe.minutes(5))[1]
    assert second.quality is CandleQuality.INCOMPLETE_VOLUME  # not authoritative
    assert second.traded_quantity == 70  # provisional 1070 - 1000, NOT a sum of LTQ


def test_provisional_volume_is_not_the_sum_of_last_traded_quantity() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick("RELIANCE", _ist(9, 16), ltq=5, cumulative=1000), _session())
    for cum in (1010, 1040, 1070):
        engine.update(_tick("RELIANCE", _ist(9, 21), ltq=5, cumulative=cum), _session())
    engine.flush(_utc(3, 55))
    assert _incomplete(engine, "RELIANCE", Timeframe.minutes(5))[1].traded_quantity == 70


def test_baseline_rolls_across_contiguous_buckets() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick("RELIANCE", _ist(9, 16), cumulative=1000), _session())  # b1 (no baseline)
    engine.update(_tick("RELIANCE", _ist(9, 21), cumulative=1070), _session())  # b2
    engine.update(_tick("RELIANCE", _ist(9, 26), cumulative=1090), _session())  # b3
    engine.flush(_utc(4, 0))
    volumes = [
        candle.traded_quantity for candle in _incomplete(engine, "RELIANCE", Timeframe.minutes(5))
    ]
    assert volumes == [None, 70, 20]  # b1 no baseline; then 1070-1000, 1090-1070


def test_zero_volume_interval_is_valid() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick("RELIANCE", _ist(9, 16), cumulative=1000), _session())
    engine.update(_tick("RELIANCE", _ist(9, 21), cumulative=1000), _session())  # no new volume
    engine.flush(_utc(3, 55))
    assert _incomplete(engine, "RELIANCE", Timeframe.minutes(5))[1].traded_quantity == 0


def test_within_session_cumulative_decrease_yields_no_volume() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick("RELIANCE", _ist(9, 16), cumulative=1000), _session())
    engine.update(_tick("RELIANCE", _ist(9, 21), cumulative=1070), _session())
    engine.update(
        _tick("RELIANCE", _ist(9, 21), cumulative=1050), _session()
    )  # decrease -> invalid
    engine.flush(_utc(3, 55))
    second = _incomplete(engine, "RELIANCE", Timeframe.minutes(5))[1]
    assert second.traded_quantity is None
    assert _authoritative(engine, "RELIANCE", Timeframe.minutes(5)) == ()


def test_missing_cumulative_volume_yields_no_volume() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick("RELIANCE", _ist(9, 16), cumulative=1000), _session())
    engine.update(_tick("RELIANCE", _ist(9, 21), cumulative=None), _session())  # no cumulative
    engine.flush(_utc(3, 55))
    assert _incomplete(engine, "RELIANCE", Timeframe.minutes(5))[1].traded_quantity is None


def test_new_trading_session_resets_the_baseline() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick("RELIANCE", _ist(9, 16), cumulative=1000), _session())
    engine.update(_tick("RELIANCE", _ist(9, 21), cumulative=1070), _session())
    next_day = date(2026, 8, 7)
    reset_tick = Tick(
        instrument=_instrument("RELIANCE"),
        event_timestamp=datetime(2026, 8, 7, 9, 16, tzinfo=_IST),
        last_price=Decimal("100"),
        session_cumulative_volume=40,
    )
    engine.update(reset_tick, _session(trading_date=next_day))
    partial = _set(engine, "RELIANCE", Timeframe.minutes(5)).partial
    assert partial is not None
    assert partial.traded_quantity is None  # no baseline in the fresh session


# --------------------------------------------------------------------------- #
# Lifecycle & gaps (§32, §17, §20)
# --------------------------------------------------------------------------- #
def test_incomplete_candle_is_immutable() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick("RELIANCE", _ist(9, 16), cumulative=1000), _session())
    engine.flush(_utc(3, 50))
    candle = _incomplete(engine, "RELIANCE", Timeframe.minutes(5))[0]
    with pytest.raises(Exception):  # noqa: B017,PT011 (frozen model rejects mutation)
        candle.traded_quantity = 999  # type: ignore[misc]


def test_gap_is_not_misattributed_and_produces_no_authoritative_candle() -> None:
    # §20 mandatory regression: 10_000 before a gap, 15_000 after -> the 5_000 delta
    # must NOT be assigned to the post-gap bucket, and no canonical Candle is emitted.
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick("RELIANCE", _ist(9, 19), cumulative=10_000), _session())  # 09:15-09:20
    engine.update(
        _tick("RELIANCE", _ist(9, 27), cumulative=15_000), _session()
    )  # 09:25-09:30 (gap)
    engine.flush(_utc(4, 0))
    by_start = {c.start_timestamp: c for c in _incomplete(engine, "RELIANCE", Timeframe.minutes(5))}
    assert _utc(3, 50) not in by_start  # empty 09:20-09:25 bucket is not fabricated
    post_gap = by_start[_utc(3, 55)]  # 09:25-09:30
    assert post_gap.quality is CandleQuality.FEED_GAP
    assert post_gap.traded_quantity is None  # the 5_000 delta is not attributed
    assert _authoritative(engine, "RELIANCE", Timeframe.minutes(5)) == ()


# --------------------------------------------------------------------------- #
# Session behavior (§33)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "state",
    [
        MarketState.PRE_OPEN,
        MarketState.OPENING_AUCTION,
        MarketState.CLOSING_SESSION,
        MarketState.MARKET_CLOSED,
        MarketState.HOLIDAY,
        MarketState.EMERGENCY_HALT,
    ],
)
def test_non_live_phases_do_not_aggregate(state: MarketState) -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick("RELIANCE", _ist(9, 16), cumulative=100), _session(state=state))
    assert _set(engine, "RELIANCE", Timeframe.minutes(5)).partial is None


def test_session_timeframe_partial_spans_the_regular_session() -> None:
    engine = _engine([Timeframe.session()])
    engine.update(_tick("RELIANCE", _ist(10, 0), cumulative=100), _session())
    partial = _set(engine, "RELIANCE", Timeframe.session()).partial
    assert partial is not None
    assert partial.start_timestamp == _utc(3, 45)  # 09:15 IST
    assert partial.end_timestamp == _utc(10, 0)  # 15:30 IST


def test_session_close_flush_finalizes_the_trailing_bucket() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick("RELIANCE", _ist(9, 16), cumulative=1000), _session())
    engine.update(_tick("RELIANCE", _ist(15, 29), cumulative=1500), _session())
    assert _set(engine, "RELIANCE", Timeframe.minutes(5)).partial is not None
    engine.flush(_utc(10, 0))  # 15:30 IST
    assert _set(engine, "RELIANCE", Timeframe.minutes(5)).partial is None


# --------------------------------------------------------------------------- #
# Isolation (§34)
# --------------------------------------------------------------------------- #
def test_instruments_are_isolated_at_the_same_timeframe() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick("RELIANCE", _ist(9, 16), price="100", cumulative=1000), _session())
    engine.update(_tick("TCS", _ist(9, 16), price="200", cumulative=5000), _session())
    reliance = _set(engine, "RELIANCE", Timeframe.minutes(5)).partial
    tcs = _set(engine, "TCS", Timeframe.minutes(5)).partial
    assert reliance is not None and tcs is not None
    assert reliance.open_price == Decimal("100")
    assert tcs.open_price == Decimal("200")


def test_timeframes_are_isolated_for_the_same_instrument() -> None:
    engine = _engine([Timeframe.minutes(5), Timeframe.minutes(15)])
    engine.update(_tick("RELIANCE", _ist(9, 16), cumulative=1000), _session())
    engine.update(_tick("RELIANCE", _ist(9, 21), cumulative=1070), _session())  # new 5m, same 15m
    five = _set(engine, "RELIANCE", Timeframe.minutes(5)).partial
    fifteen = _set(engine, "RELIANCE", Timeframe.minutes(15)).partial
    assert five is not None and fifteen is not None
    assert five.start_timestamp == _utc(3, 50)  # advanced to 09:20 bucket
    assert fifteen.start_timestamp == _utc(3, 45)  # still first 15m bucket


# --------------------------------------------------------------------------- #
# Determinism / replay (§35)
# --------------------------------------------------------------------------- #
def _replay_run() -> list[tuple[str, int | None, str, str]]:
    engine = _engine([Timeframe.minutes(1), Timeframe.minutes(5)])
    ticks = [
        (_ist(9, 16), "100", 1000),
        (_ist(9, 17), "102", 1030),
        (_ist(9, 21), "101", 1070),
        (_ist(9, 26), "103", 1090),
    ]
    for at, price, cum in ticks:
        engine.update(_tick("RELIANCE", at, price=price, cumulative=cum), _session())
    engine.flush(_utc(4, 0))
    result: list[tuple[str, int | None, str, str]] = []
    for candles in engine.candle_sets_for(_instrument("RELIANCE")):
        for candle in candles.incomplete:
            result.append(
                (
                    candle.quality.value,
                    candle.traded_quantity,
                    str(candle.close_price),
                    candle.start_timestamp.isoformat(),
                )
            )
    return result


def test_identical_input_replays_identically() -> None:
    assert _replay_run() == _replay_run()
