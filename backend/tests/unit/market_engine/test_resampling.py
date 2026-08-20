"""Exact historical resampling: divisibility, aggregation, and rejection (P4.5C; §33-35, 37)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.market_engine.historical.context import HistoricalSeries
from app.market_engine.historical.resampling import (
    divides_exactly,
    reconstruct_series,
    select_base,
)
from app.market_engine.session import EffectiveSchedule, SessionSchedule, TradingCalendar
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, Instrument

_IST = ZoneInfo("Asia/Kolkata")
_TZ = "Asia/Kolkata"
_DATE = date(2026, 8, 6)  # Thursday, a trading day
_OPEN = datetime.combine(_DATE, time(9, 15), tzinfo=_IST)
_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)
_CALENDAR = TradingCalendar()
_ONE = Timeframe.minutes(1)
_SEVEN = Timeframe.minutes(7)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _minute(
    offset: int,
    *,
    symbol: str = "RELIANCE",
    width: int = 1,
    high: str = "101",
    low: str = "99",
    open_price: str = "100",
    close: str = "100",
    quantity: int = 10,
    day: date = _DATE,
) -> Candle:
    start = datetime.combine(day, time(9, 15), tzinfo=_IST) + timedelta(minutes=offset)
    start_utc = start.astimezone(UTC)
    return Candle(
        instrument=_instrument(symbol),
        start_timestamp=start_utc,
        end_timestamp=start_utc + timedelta(minutes=width),
        open_price=Decimal(open_price),
        high_price=Decimal(high),
        low_price=Decimal(low),
        close_price=Decimal(close),
        traded_quantity=quantity,
    )


def _source(candles: tuple[Candle, ...], timeframe: Timeframe = _ONE) -> HistoricalSeries:
    return HistoricalSeries(timeframe=timeframe, candles=candles)


def _reconstruct(source: HistoricalSeries, target: Timeframe) -> HistoricalSeries | None:
    return reconstruct_series(
        source=source,
        target=target,
        effective=EffectiveSchedule(default=_SCHEDULE),
        calendar=_CALENDAR,
        exchange_timezone=_TZ,
    )


# --------------------------------------------------------------------------- #
# Divisibility (§33)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("target", "base", "expected"),
    [
        (Timeframe.minutes(5), Timeframe.minutes(1), True),
        (Timeframe.minutes(7), Timeframe.minutes(1), True),
        (Timeframe.minutes(15), Timeframe.minutes(1), True),
        (Timeframe.minutes(10), Timeframe.minutes(5), True),
        (Timeframe.minutes(15), Timeframe.minutes(5), True),
        (Timeframe.minutes(7), Timeframe.minutes(5), False),
    ],
)
def test_divisibility(target: Timeframe, base: Timeframe, expected: bool) -> None:
    assert divides_exactly(target, base) is expected


def test_reconstruct_rejects_non_divisible_pair() -> None:
    source = _source((_minute(0),), timeframe=Timeframe.minutes(5))
    with pytest.raises(ValueError, match="exactly divisible"):
        _reconstruct(source, Timeframe.minutes(7))


# --------------------------------------------------------------------------- #
# 7-minute reconstruction (§34)
# --------------------------------------------------------------------------- #
def test_single_seven_minute_bucket() -> None:
    source = _source(tuple(_minute(offset) for offset in range(7)))
    result = _reconstruct(source, _SEVEN)
    assert result is not None
    assert len(result.candles) == 1
    candle = result.candles[0]
    assert candle.start_timestamp == _OPEN.astimezone(UTC)
    assert candle.end_timestamp == (_OPEN + timedelta(minutes=7)).astimezone(UTC)


def test_multiple_sequential_seven_minute_buckets() -> None:
    source = _source(tuple(_minute(offset) for offset in range(14)))
    result = _reconstruct(source, _SEVEN)
    assert result is not None
    assert len(result.candles) == 2
    assert result.candles[1].start_timestamp == (_OPEN + timedelta(minutes=7)).astimezone(UTC)


def test_ohlc_and_volume_aggregation() -> None:
    candles = (
        _minute(0, open_price="100", high="102", low="98", close="101", quantity=5),
        _minute(1, open_price="101", high="105", low="97", close="103", quantity=7),
        *(_minute(offset, quantity=1) for offset in range(2, 7)),
    )
    result = _reconstruct(_source(candles), _SEVEN)
    assert result is not None
    candle = result.candles[0]
    assert candle.open_price == Decimal("100")  # first source open
    assert candle.high_price == Decimal("105")  # max high
    assert candle.low_price == Decimal("97")  # min low
    assert candle.close_price == Decimal("100")  # last source close (offset 6 default 100)
    assert candle.traded_quantity == 5 + 7 + 5  # exact sum


def test_final_truncated_session_bucket_is_reconstructed() -> None:
    # Full session of 1m candles (375 minutes). The last 7m bucket is 15:26-15:30.
    source = _source(tuple(_minute(offset) for offset in range(375)))
    result = _reconstruct(source, _SEVEN)
    assert result is not None
    last = result.candles[-1]
    assert last.end_timestamp == datetime.combine(_DATE, time(15, 30), tzinfo=_IST).astimezone(UTC)
    assert last.start_timestamp == datetime.combine(_DATE, time(15, 26), tzinfo=_IST).astimezone(
        UTC
    )


# --------------------------------------------------------------------------- #
# Exactness failures — each withholds authority (§35)
# --------------------------------------------------------------------------- #
def test_missing_constituent_withholds() -> None:
    candles = tuple(_minute(offset) for offset in range(7) if offset != 3)
    assert _reconstruct(_source(candles), _SEVEN) is None


def test_wrong_source_width_withholds() -> None:
    candles = (_minute(0, width=2), *(_minute(offset) for offset in range(2, 7)))
    assert _reconstruct(_source(candles), _SEVEN) is None


def test_misaligned_source_start_withholds() -> None:
    candles = tuple(_minute(offset) for offset in range(1, 7))  # starts at 09:16, not 09:15
    assert _reconstruct(_source(candles), _SEVEN) is None


def test_source_crossing_target_boundary_withholds() -> None:
    candles = (*(_minute(offset) for offset in range(6)), _minute(6, width=2))  # ends 09:23 > 09:22
    assert _reconstruct(_source(candles), _SEVEN) is None


def test_incomplete_truncated_final_bucket_withholds() -> None:
    # Full session minus its last minute (15:29-15:30). Every full 7m bucket
    # reconstructs, but the truncated final bucket 15:26-15:30 is withheld.
    source = _source(tuple(_minute(offset) for offset in range(374)))  # misses 15:29-15:30
    result = _reconstruct(source, _SEVEN)
    assert result is not None
    close = datetime.combine(_DATE, time(15, 30), tzinfo=_IST).astimezone(UTC)
    assert all(candle.end_timestamp != close for candle in result.candles)
    assert result.candles[-1].end_timestamp == datetime.combine(
        _DATE, time(15, 26), tzinfo=_IST
    ).astimezone(UTC)


def test_holiday_dated_source_withholds() -> None:
    calendar = TradingCalendar(holidays=(_DATE,))
    source = _source(tuple(_minute(offset) for offset in range(7)))
    result = reconstruct_series(
        source=source,
        target=_SEVEN,
        effective=EffectiveSchedule(default=_SCHEDULE),
        calendar=calendar,
        exchange_timezone=_TZ,
    )
    assert result is None


def test_cross_session_candles_never_merge() -> None:
    thursday = tuple(_minute(offset) for offset in range(7))
    friday = tuple(_minute(offset, day=date(2026, 8, 7)) for offset in range(7))
    result = _reconstruct(_source(thursday + friday), _SEVEN)
    assert result is not None
    assert len(result.candles) == 2  # one per trading date, never spanning the gap
    assert result.candles[0].start_timestamp == _OPEN.astimezone(UTC)
    assert result.candles[1].start_timestamp == datetime.combine(
        date(2026, 8, 7), time(9, 15), tzinfo=_IST
    ).astimezone(UTC)


def test_duplicate_and_overlap_rejected_at_the_source_boundary() -> None:
    duplicate = _minute(0)
    with pytest.raises(ValueError, match="duplicate"):
        HistoricalSeries(timeframe=_ONE, candles=(duplicate, duplicate))
    with pytest.raises(ValueError, match="overlapping"):
        HistoricalSeries(timeframe=_ONE, candles=(_minute(0, width=3), _minute(1)))


# --------------------------------------------------------------------------- #
# Base selection (§37)
# --------------------------------------------------------------------------- #
def test_base_selection_prefers_largest_exact_divisor() -> None:
    assert select_base(Timeframe.minutes(10), frozenset({_ONE, Timeframe.minutes(5)})) == (
        Timeframe.minutes(5)
    )
    assert select_base(_SEVEN, frozenset({_ONE, Timeframe.minutes(5)})) == _ONE
    assert select_base(
        Timeframe.minutes(30), frozenset({_ONE, Timeframe.minutes(5), Timeframe.minutes(15)})
    ) == Timeframe.minutes(15)


def test_base_selection_returns_none_when_no_exact_base() -> None:
    assert select_base(_SEVEN, frozenset({Timeframe.minutes(5), Timeframe.minutes(15)})) is None


def test_base_selection_returns_none_for_session() -> None:
    assert select_base(Timeframe.session(), frozenset({_ONE, Timeframe.minutes(5)})) is None
