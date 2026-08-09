"""Exact historical reconstruction of authoritative candles (P4.5C).

Aggregates an authoritative smaller-timeframe :class:`HistoricalSeries` (e.g. 1m)
into a larger session-aligned timeframe (e.g. 7m). Buckets come from the one shared
:mod:`app.market_engine.buckets` algorithm, so reconstructed boundaries are identical
to the live engine's. A target candle is emitted only when its constituents *exactly*
and contiguously cover the bucket (ADR-006 exactness); any gap, overlap, misalignment,
wrong source width, cross-session span, or holiday-dated source withholds that bucket.
Nothing is interpolated, filled, or fabricated. This module is pure — no provider I/O.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.market_engine.buckets import bucket_bounds, session_buckets
from app.market_engine.historical.context import HistoricalSeries
from app.market_engine.session import SessionSchedule, TradingCalendar
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle


def divides_exactly(target: Timeframe, base: Timeframe) -> bool:
    """Return whether an intraday ``base`` duration exactly divides an intraday ``target``."""
    if target.duration is None or base.duration is None:
        return False
    if base.duration >= target.duration:
        return False
    return target.duration % base.duration == timedelta(0)


def select_base(target: Timeframe, direct: frozenset[Timeframe]) -> Timeframe | None:
    """Return the largest directly-supported intraday timeframe that exactly builds target.

    Args:
        target: The reconstruction-pending timeframe.
        direct: Timeframes the source supports directly.

    Returns:
        The chosen base timeframe, or ``None`` when no exact intraday base exists
        (including when ``target`` is itself the session timeframe).
    """
    if target.is_session or target.duration is None:
        return None
    candidates = [base for base in direct if divides_exactly(target, base)]
    if not candidates:
        return None
    return max(candidates, key=lambda base: base.duration or timedelta(0))


def reconstruct_series(
    *,
    source: HistoricalSeries,
    target: Timeframe,
    schedule: SessionSchedule,
    calendar: TradingCalendar,
    exchange_timezone: str,
) -> HistoricalSeries | None:
    """Reconstruct an authoritative target series from a smaller authoritative source.

    Args:
        source: The authoritative smaller-timeframe series (one instrument).
        target: The intraday target timeframe (must be exactly divisible by source).
        schedule: The exchange session boundaries (bucket anchor and truncation).
        calendar: The trading calendar (holiday protection).
        exchange_timezone: The IANA exchange timezone.

    Returns:
        The reconstructed series (only exactly-covered buckets), or ``None`` when no
        bucket can be proven exact.

    Raises:
        ValueError: If ``target`` is not exactly divisible by the source timeframe.
    """
    if not divides_exactly(target, source.timeframe):
        raise ValueError("target timeframe must be exactly divisible by the source timeframe")
    source_duration = source.timeframe.duration
    if source_duration is None:  # unreachable after divides_exactly; satisfies typing
        return None
    timezone = ZoneInfo(exchange_timezone)
    by_date = _group_by_trading_date(source.candles, timezone)
    reconstructed: list[Candle] = []
    for trading_date in sorted(by_date):
        if not calendar.is_trading_day(trading_date):
            continue  # holiday-dated source bars are a conflict — withhold (§14)
        reconstructed.extend(
            _reconstruct_session(
                trading_date=trading_date,
                day_candles=by_date[trading_date],
                target=target,
                source_duration=source_duration,
                schedule=schedule,
                timezone=timezone,
            )
        )
    if not reconstructed:
        return None
    return HistoricalSeries(timeframe=target, candles=tuple(reconstructed))


def _group_by_trading_date(
    candles: tuple[Candle, ...], timezone: ZoneInfo
) -> dict[date, list[Candle]]:
    grouped: dict[date, list[Candle]] = defaultdict(list)
    for candle in candles:
        grouped[candle.start_timestamp.astimezone(timezone).date()].append(candle)
    return grouped


def _reconstruct_session(
    *,
    trading_date: date,
    day_candles: list[Candle],
    target: Timeframe,
    source_duration: timedelta,
    schedule: SessionSchedule,
    timezone: ZoneInfo,
) -> list[Candle]:
    _, _, session_close = bucket_bounds(
        event_timestamp=datetime.combine(trading_date, schedule.regular_close, tzinfo=timezone),
        trading_date=trading_date,
        timeframe=Timeframe.minutes(1),
        schedule=schedule,
        timezone=timezone,
    )
    by_index = _group_by_bucket_index(day_candles, trading_date, target, schedule, timezone)
    emitted: list[Candle] = []
    for index, start, end in session_buckets(
        trading_date=trading_date, timeframe=target, schedule=schedule, timezone=timezone
    ):
        candle = _prove_and_aggregate(
            constituents=by_index.get(index, []),
            start=start,
            end=end,
            source_duration=source_duration,
            session_close=session_close,
        )
        if candle is not None:
            emitted.append(candle)
    return emitted


def _group_by_bucket_index(
    day_candles: list[Candle],
    trading_date: date,
    target: Timeframe,
    schedule: SessionSchedule,
    timezone: ZoneInfo,
) -> dict[int, list[Candle]]:
    grouped: dict[int, list[Candle]] = defaultdict(list)
    for candle in day_candles:
        index, _, _ = bucket_bounds(
            event_timestamp=candle.start_timestamp,
            trading_date=trading_date,
            timeframe=target,
            schedule=schedule,
            timezone=timezone,
        )
        grouped[index].append(candle)
    return grouped


def _prove_and_aggregate(
    *,
    constituents: list[Candle],
    start: datetime,
    end: datetime,
    source_duration: timedelta,
    session_close: datetime,
) -> Candle | None:
    if not constituents:
        return None
    ordered = sorted(constituents, key=lambda candle: candle.start_timestamp)
    if ordered[0].start_timestamp != start or ordered[-1].end_timestamp != end:
        return None
    if not _contiguous_and_sized(ordered, source_duration, session_close):
        return None
    return Candle(
        instrument=ordered[0].instrument,
        start_timestamp=start,
        end_timestamp=end,
        open_price=ordered[0].open_price,
        high_price=max(candle.high_price for candle in ordered),
        low_price=min(candle.low_price for candle in ordered),
        close_price=ordered[-1].close_price,
        traded_quantity=sum(candle.traded_quantity for candle in ordered),
    )


def _contiguous_and_sized(
    ordered: list[Candle], source_duration: timedelta, session_close: datetime
) -> bool:
    for position, candle in enumerate(ordered):
        width = candle.end_timestamp - candle.start_timestamp
        if width != source_duration and candle.end_timestamp != session_close:
            return False
        if position > 0 and candle.start_timestamp != ordered[position - 1].end_timestamp:
            return False
    return True
