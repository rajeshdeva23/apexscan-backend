"""Session-relative candle bucket boundaries — the one shared algorithm (docs/06 §13).

Both the live :class:`~app.market_engine.candle_engine.CandleEngine` and historical
reconstruction (P4.5C) must place candles in identical buckets. That algorithm lives
here, once: buckets are anchored at the P4.3 ``regular_open`` (never the Unix epoch),
each spans the timeframe's duration, and the final bucket is truncated at
``regular_close``. Pure and deterministic — no wall-clock, no provider knowledge.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.market_engine.session import SessionSchedule
from app.market_engine.timeframe import Timeframe


def bucket_bounds(
    *,
    event_timestamp: datetime,
    trading_date: date,
    timeframe: Timeframe,
    schedule: SessionSchedule,
    timezone: ZoneInfo,
) -> tuple[int, datetime, datetime]:
    """Return the ``(index, start_utc, end_utc)`` of the bucket containing an instant.

    For the whole-session timeframe the single bucket spans ``[regular_open,
    regular_close)``. For an intraday timeframe the bucket is anchored at
    ``regular_open``; the last bucket's end is truncated at ``regular_close``.

    Args:
        event_timestamp: A timezone-aware instant to locate.
        trading_date: The exchange-local trading date the session belongs to.
        timeframe: The timeframe whose bucketing applies.
        schedule: The exchange session boundaries.
        timezone: The exchange timezone.

    Returns:
        The bucket index (0-based from the open) and its UTC half-open bounds.
    """
    open_local = datetime.combine(trading_date, schedule.regular_open, tzinfo=timezone)
    close_local = datetime.combine(trading_date, schedule.regular_close, tzinfo=timezone)
    if timeframe.duration is None:
        return 0, open_local.astimezone(UTC), close_local.astimezone(UTC)
    local = event_timestamp.astimezone(timezone)
    index = (local - open_local) // timeframe.duration
    start_local = open_local + index * timeframe.duration
    end_local = min(start_local + timeframe.duration, close_local)
    return index, start_local.astimezone(UTC), end_local.astimezone(UTC)


def session_buckets(
    *,
    trading_date: date,
    timeframe: Timeframe,
    schedule: SessionSchedule,
    timezone: ZoneInfo,
) -> tuple[tuple[int, datetime, datetime], ...]:
    """Return every bucket covering one regular session, in order.

    Args:
        trading_date: The exchange-local trading date.
        timeframe: The timeframe whose buckets to enumerate.
        schedule: The exchange session boundaries.
        timezone: The exchange timezone.

    Returns:
        An ordered tuple of ``(index, start_utc, end_utc)`` covering the session;
        the last bucket is truncated at ``regular_close``.
    """
    open_local = datetime.combine(trading_date, schedule.regular_open, tzinfo=timezone)
    close_local = datetime.combine(trading_date, schedule.regular_close, tzinfo=timezone)
    if timeframe.duration is None:
        return ((0, open_local.astimezone(UTC), close_local.astimezone(UTC)),)
    buckets: list[tuple[int, datetime, datetime]] = []
    index = 0
    cursor = open_local
    while cursor < close_local:
        end_local = min(cursor + timeframe.duration, close_local)
        buckets.append((index, cursor.astimezone(UTC), end_local.astimezone(UTC)))
        cursor = cursor + timeframe.duration
        index += 1
    return tuple(buckets)
