"""Feed-continuity and candle-completeness corrections (ADR-006; P4.4B)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.market_engine.candle_engine import CandleEngine
from app.market_engine.context import CandleQuality, MarketState, SessionContext
from app.market_engine.session import SessionSchedule
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import FeedContinuity, FeedContinuityEvent, Instrument, Tick

_IST = ZoneInfo("Asia/Kolkata")
_OBSERVED = datetime(2026, 8, 6, 7, 0, tzinfo=UTC)
_SCHEDULE = SessionSchedule(
    pre_open_start=datetime(2000, 1, 1, 9, 0).time(),
    opening_auction_start=datetime(2000, 1, 1, 9, 8).time(),
    regular_open=datetime(2000, 1, 1, 9, 15).time(),
    regular_close=datetime(2000, 1, 1, 15, 30).time(),
    closing_end=datetime(2000, 1, 1, 15, 40).time(),
)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _session(*, trading_date: date = date(2026, 8, 6)) -> SessionContext:
    return SessionContext(
        trading_date=trading_date,
        market_state=MarketState.LIVE_SESSION,
        exchange_timezone="Asia/Kolkata",
    )


def _tick(at: datetime, *, symbol: str = "RELIANCE", cumulative: int | None = None) -> Tick:
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=at,
        last_price=Decimal("100"),
        session_cumulative_volume=cumulative,
    )


def _ist(hour: int, minute: int) -> datetime:
    return datetime(2026, 8, 6, hour, minute, tzinfo=_IST)


def _engine(timeframes: list[Timeframe]) -> CandleEngine:
    return CandleEngine(schedule=_SCHEDULE, exchange_timezone="Asia/Kolkata", timeframes=timeframes)


def _loss() -> FeedContinuityEvent:
    return FeedContinuityEvent(status=FeedContinuity.CONTINUITY_LOST, observed_at=_OBSERVED)


def _reconnected() -> FeedContinuityEvent:
    return FeedContinuityEvent(status=FeedContinuity.RECONNECTED, observed_at=_OBSERVED)


def _set(engine: CandleEngine, timeframe: Timeframe, symbol: str = "RELIANCE"):  # noqa: ANN202
    return next(c for c in engine.candle_sets_for(_instrument(symbol)) if c.timeframe == timeframe)


def test_within_bucket_continuity_loss_marks_the_interval_incomplete_ohlc() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick(_ist(9, 16), cumulative=1000), _session())
    engine.record_continuity(_loss())
    engine.update(_tick(_ist(9, 17), cumulative=1005), _session())  # same bucket, post-loss
    engine.flush(datetime(2026, 8, 6, 3, 50, tzinfo=UTC))  # close 09:15-09:20
    incomplete = _set(engine, Timeframe.minutes(5)).incomplete
    assert incomplete[0].quality is CandleQuality.INCOMPLETE_OHLC
    assert _set(engine, Timeframe.minutes(5)).finalized == ()


def test_continuity_loss_spanning_buckets_yields_feed_gap_with_no_baseline() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick(_ist(9, 16), cumulative=1000), _session())  # b0
    engine.record_continuity(_loss())
    engine.update(_tick(_ist(9, 21), cumulative=9999), _session())  # b1 opens post-loss
    engine.flush(datetime(2026, 8, 6, 3, 55, tzinfo=UTC))
    incomplete = _set(engine, Timeframe.minutes(5)).incomplete
    post_loss = incomplete[-1]
    assert post_loss.quality is CandleQuality.FEED_GAP
    assert post_loss.traded_quantity is None  # pre-loss baseline is not reused


def test_reconnect_does_not_restore_completeness() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick(_ist(9, 16), cumulative=1000), _session())
    engine.record_continuity(_loss())
    engine.record_continuity(_reconnected())  # reconnect must not clear the taint
    engine.update(_tick(_ist(9, 21), cumulative=1070), _session())
    engine.flush(datetime(2026, 8, 6, 3, 55, tzinfo=UTC))
    post = _set(engine, Timeframe.minutes(5)).incomplete[-1]
    assert post.quality is CandleQuality.FEED_GAP
    assert post.traded_quantity is None


def test_one_continuity_loss_invalidates_every_active_timeframe() -> None:
    timeframes = [
        Timeframe.minutes(1),
        Timeframe.minutes(5),
        Timeframe.minutes(7),
        Timeframe.minutes(15),
    ]
    engine = _engine(timeframes)
    engine.update(_tick(_ist(9, 16), cumulative=1000), _session())
    engine.record_continuity(_loss())
    for timeframe in timeframes:
        partial = _set(engine, timeframe).partial
        assert partial is not None
        assert partial.quality is CandleQuality.INCOMPLETE_OHLC


def test_new_trading_session_clears_the_continuity_taint() -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick(_ist(9, 16), cumulative=1000), _session())
    engine.record_continuity(_loss())
    next_day = date(2026, 8, 7)
    fresh = Tick(
        instrument=_instrument(),
        event_timestamp=datetime(2026, 8, 7, 9, 16, tzinfo=_IST),
        last_price=Decimal("100"),
        session_cumulative_volume=50,
    )
    engine.update(fresh, _session(trading_date=next_day))
    partial = _set(engine, Timeframe.minutes(5)).partial
    assert partial is not None
    assert partial.quality is CandleQuality.INCOMPLETE_VOLUME  # fresh session, not tainted


@pytest.mark.parametrize("status", [FeedContinuity.CONNECTED, FeedContinuity.RECONNECTED])
def test_non_loss_continuity_facts_do_not_taint(status: FeedContinuity) -> None:
    engine = _engine([Timeframe.minutes(5)])
    engine.update(_tick(_ist(9, 16), cumulative=1000), _session())
    engine.record_continuity(FeedContinuityEvent(status=status, observed_at=_OBSERVED))
    partial = _set(engine, Timeframe.minutes(5)).partial
    assert partial is not None
    assert partial.quality is CandleQuality.INCOMPLETE_VOLUME  # not tainted by a non-loss fact
