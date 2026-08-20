"""Live P4.4 and historical P4.5C place candles in identical buckets (P4.5C; §36)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.market_engine.buckets import bucket_bounds
from app.market_engine.candle_engine import CandleEngine
from app.market_engine.context import MarketState, SessionContext
from app.market_engine.session import SessionSchedule
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument, Tick

_IST = ZoneInfo("Asia/Kolkata")
_DATE = date(2026, 8, 6)
_TZ = "Asia/Kolkata"
_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)


def _instrument() -> Instrument:
    return Instrument(exchange="NSE", symbol="RELIANCE")


def _session() -> SessionContext:
    return SessionContext(
        trading_date=_DATE, market_state=MarketState.LIVE_SESSION, exchange_timezone=_TZ
    )


def _tick(at: datetime) -> Tick:
    return Tick(
        instrument=_instrument(),
        event_timestamp=at,
        last_price=Decimal("100"),
        traded_quantity=1,
        session_cumulative_volume=100,
    )


def _live_partial_bounds(timeframe: Timeframe, at: datetime) -> tuple[datetime, datetime]:
    engine = CandleEngine(schedule=_SCHEDULE, exchange_timezone=_TZ, timeframes=[timeframe])
    engine.update(_tick(at), _session())
    partial = next(
        candles.partial
        for candles in engine.candle_sets_for(_instrument())
        if candles.timeframe == timeframe
    )
    assert partial is not None
    return partial.start_timestamp, partial.end_timestamp


@pytest.mark.parametrize(
    ("minutes", "at"),
    [
        (1, datetime(2026, 8, 6, 9, 16, tzinfo=_IST)),
        (5, datetime(2026, 8, 6, 9, 24, tzinfo=_IST)),
        (7, datetime(2026, 8, 6, 9, 30, tzinfo=_IST)),
        (15, datetime(2026, 8, 6, 9, 44, tzinfo=_IST)),
    ],
)
def test_live_and_historical_buckets_match(minutes: int, at: datetime) -> None:
    timeframe = Timeframe.minutes(minutes)
    partial_start, partial_end = _live_partial_bounds(timeframe, at)
    _, shared_start, shared_end = bucket_bounds(
        event_timestamp=at,
        trading_date=_DATE,
        timeframe=timeframe,
        interval=_SCHEDULE.bounds,
        timezone=_IST,
    )
    assert (partial_start, partial_end) == (shared_start, shared_end)


def test_live_and_historical_final_truncated_bucket_match() -> None:
    # 7m does not divide the 375-minute session; the last bucket ends at 15:30.
    timeframe = Timeframe.minutes(7)
    at = datetime(2026, 8, 6, 15, 29, tzinfo=_IST)
    partial_start, partial_end = _live_partial_bounds(timeframe, at)
    _, shared_start, shared_end = bucket_bounds(
        event_timestamp=at,
        trading_date=_DATE,
        timeframe=timeframe,
        interval=_SCHEDULE.bounds,
        timezone=_IST,
    )
    assert partial_end == datetime(2026, 8, 6, 15, 30, tzinfo=_IST).astimezone(UTC)
    assert (partial_start, partial_end) == (shared_start, shared_end)
