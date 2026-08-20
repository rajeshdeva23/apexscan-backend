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

from app.market_engine.session import TradingInterval
from app.market_engine.timeframe import Timeframe


def bucket_bounds(
    *,
    event_timestamp: datetime,
    trading_date: date,
    timeframe: Timeframe,
    interval: TradingInterval,
    timezone: ZoneInfo,
) -> tuple[int, datetime, datetime]:
    """Return the ``(index, start_utc, end_utc)`` of the bucket containing an instant.

    For the whole-session timeframe the single bucket spans ``[interval.start,
    interval.end)``. For an intraday timeframe the bucket is anchored at
    ``interval.start``; the last bucket's end is truncated at ``interval.end``.

    Args:
        event_timestamp: A timezone-aware instant to locate.
        trading_date: The exchange-local trading date the session belongs to.
        timeframe: The timeframe whose bucketing applies.
        interval: The exchange live interval for this bucketing (default or override).
        timezone: The exchange timezone.

    Returns:
        The bucket index (0-based from the interval start) and its UTC half-open bounds.
    """
    open_local = datetime.combine(trading_date, interval.start, tzinfo=timezone)
    close_local = datetime.combine(trading_date, interval.end, tzinfo=timezone)
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
    interval: TradingInterval,
    timezone: ZoneInfo,
) -> tuple[tuple[int, datetime, datetime], ...]:
    """Return every bucket covering one live interval, in order.

    Args:
        trading_date: The exchange-local trading date.
        timeframe: The timeframe whose buckets to enumerate.
        interval: The exchange live interval for this bucketing (default or override).
        timezone: The exchange timezone.

    Returns:
        An ordered tuple of ``(index, start_utc, end_utc)`` covering the interval;
        the last bucket is truncated at ``interval.end``.
    """
    open_local = datetime.combine(trading_date, interval.start, tzinfo=timezone)
    close_local = datetime.combine(trading_date, interval.end, tzinfo=timezone)
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


def day_buckets(
    *,
    trading_date: date,
    timeframe: Timeframe,
    intervals: tuple[TradingInterval, ...],
    timezone: ZoneInfo,
) -> tuple[tuple[int, datetime, datetime], ...]:
    """Return every bucket of a whole trading day across one or more live intervals.

    Each interval is bucketed independently (anchored at its own start, final bucket
    truncated at its own end) via :func:`session_buckets`, then the per-interval
    buckets are concatenated chronologically and assigned a single contiguous 0-based
    global index across the whole day. The closed gap between intervals consumes no
    index and no bucket spans it (ADR-011 multi-interval addendum MI8/MI9). A
    single-element ``intervals`` tuple is byte-identical to :func:`session_buckets`.

    Args:
        trading_date: The exchange-local trading date.
        timeframe: The timeframe whose buckets to enumerate.
        intervals: The ordered, disjoint live intervals of the day (``len >= 1``).
        timezone: The exchange timezone.

    Returns:
        An ordered tuple of ``(global_index, start_utc, end_utc)`` covering every
        live interval of the day, contiguously indexed across the whole day.
    """
    buckets: list[tuple[int, datetime, datetime]] = []
    index = 0
    for interval in intervals:
        for _local_index, start_utc, end_utc in session_buckets(
            trading_date=trading_date, timeframe=timeframe, interval=interval, timezone=timezone
        ):
            buckets.append((index, start_utc, end_utc))
            index += 1
    return tuple(buckets)
