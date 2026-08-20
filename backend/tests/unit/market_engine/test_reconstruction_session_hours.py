"""Reconstruction over exceptional OPEN sessions uses per-date effective bounds.

A special session is bucketed at its override open (not the default open); a
shortened session yields fewer buckets, truncated at the override close; and a
special OPEN date lacking session-hours metadata is withheld so it can never yield a
falsely-complete intraday session (ADR-011 addendum M16). Synthetic dates only.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.market_engine.historical.context import HistoricalSeries
from app.market_engine.historical.resampling import reconstruct_series
from app.market_engine.session import (
    EffectiveSchedule,
    SessionSchedule,
    TradingCalendar,
    TradingSessionOverride,
)
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, Instrument

_IST = ZoneInfo("Asia/Kolkata")
_TZ = "Asia/Kolkata"
_SATURDAY = date(2026, 8, 8)  # exceptional OPEN session
_ONE = Timeframe.minutes(1)
_FIVE = Timeframe.minutes(5)
_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)


def _instrument() -> Instrument:
    return Instrument(exchange="NSE", symbol="RELIANCE")


def _utc(moment: time) -> datetime:
    return datetime.combine(_SATURDAY, moment, tzinfo=_IST).astimezone(UTC)


def _minute(open_time: time, offset: int) -> Candle:
    start = datetime.combine(_SATURDAY, open_time, tzinfo=_IST) + timedelta(minutes=offset)
    start_utc = start.astimezone(UTC)
    return Candle(
        instrument=_instrument(),
        start_timestamp=start_utc,
        end_timestamp=start_utc + timedelta(minutes=1),
        open_price=Decimal("100"),
        high_price=Decimal("101"),
        low_price=Decimal("99"),
        close_price=Decimal("100"),
        traded_quantity=10,
    )


def _reconstruct(
    candles: tuple[Candle, ...], *, override: TradingSessionOverride | None
) -> HistoricalSeries | None:
    overrides = (override,) if override is not None else ()
    return reconstruct_series(
        source=HistoricalSeries(timeframe=_ONE, candles=candles),
        target=_FIVE,
        effective=EffectiveSchedule(default=_SCHEDULE, overrides=overrides),
        calendar=TradingCalendar(open_sessions=(_SATURDAY,)),
        exchange_timezone=_TZ,
    )


def test_special_session_buckets_anchored_at_override_open() -> None:
    override = TradingSessionOverride.continuous(
        trading_date=_SATURDAY, start=time(10, 0), end=time(14, 0)
    )
    source = tuple(_minute(time(10, 0), offset) for offset in range(10))
    result = _reconstruct(source, override=override)
    assert result is not None
    assert len(result.candles) == 2  # 10:00-10:05, 10:05-10:10
    first = result.candles[0]
    assert first.start_timestamp == _utc(time(10, 0))
    assert first.end_timestamp == _utc(time(10, 5))


def test_shortened_session_bucket_count_truncates_at_override_close() -> None:
    # A 12-minute session yields 10:00-10:05, 10:05-10:10, and the truncated
    # 10:10-10:12 bucket.
    override = TradingSessionOverride.continuous(
        trading_date=_SATURDAY, start=time(10, 0), end=time(10, 12)
    )
    source = tuple(_minute(time(10, 0), offset) for offset in range(12))
    result = _reconstruct(source, override=override)
    assert result is not None
    assert len(result.candles) == 3
    assert result.candles[-1].end_timestamp == _utc(time(10, 12))


def test_missing_timing_withholds_special_session() -> None:
    # No override for the OPEN Saturday: reconstruction withholds the whole date,
    # so no falsely-complete intraday session is produced.
    source = tuple(_minute(time(9, 15), offset) for offset in range(10))
    assert _reconstruct(source, override=None) is None
