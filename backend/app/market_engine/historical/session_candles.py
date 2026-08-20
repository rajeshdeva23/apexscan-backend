"""Canonicalize provider daily history into session-identity candles (P4.5E).

A provider daily bar carries an arbitrary within-day (or +1-day) timestamp, which
does not match the live whole-session candle's identity of ``[regular_open,
regular_close)``. To let historical session series, the live session candle, and
reconciliation share one exact :class:`~app.market_engine.historical.reconciliation.CandleIdentity`,
each daily bar is re-stamped to its trading date's regular-session bounds using the
one shared bucket algorithm (:mod:`app.market_engine.buckets`). OHLCV is preserved;
only the timestamps are canonicalized. A bar on a configured non-trading day is
withheld (fail closed). This is broker-neutral — no provider identifiers.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from zoneinfo import ZoneInfo

from app.market_engine.buckets import bucket_bounds
from app.market_engine.session import EffectiveSchedule, TradingCalendar
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle

_SESSION = Timeframe.session()


def canonicalize_session_candle(
    candle: Candle,
    *,
    effective: EffectiveSchedule,
    calendar: TradingCalendar,
    exchange_timezone: str,
) -> Candle | None:
    """Re-stamp a daily bar to its trading date's whole-session envelope bounds.

    The whole-session identity spans the date's envelope — the default bounds for an
    ordinary date, or the first-interval start to last-interval end for a special
    multi-interval date (ADR-011 addendum MI10). OHLCV from the daily bar is preserved
    unchanged across the intervening closure; only the timestamps are canonicalized.

    Args:
        candle: The provider daily candle (OHLCV authoritative; timestamps arbitrary).
        effective: The default-plus-override effective schedule (envelope lookup).
        calendar: The trading calendar (holiday/weekend fail-closed).
        exchange_timezone: The IANA exchange timezone.

    Returns:
        A canonical session candle spanning the trading date's envelope, or ``None``
        if that date is not a configured trading day.
    """
    timezone = ZoneInfo(exchange_timezone)
    trading_date = candle.start_timestamp.astimezone(timezone).date()
    if not calendar.is_trading_day(trading_date):
        return None
    _, start_utc, end_utc = bucket_bounds(
        event_timestamp=candle.start_timestamp,
        trading_date=trading_date,
        timeframe=_SESSION,
        interval=effective.envelope_for(trading_date),
        timezone=timezone,
    )
    return Candle(
        instrument=candle.instrument,
        start_timestamp=start_utc,
        end_timestamp=end_utc,
        open_price=candle.open_price,
        high_price=candle.high_price,
        low_price=candle.low_price,
        close_price=candle.close_price,
        traded_quantity=candle.traded_quantity,
    )


def canonical_session_series(
    candles: Iterable[Candle],
    *,
    effective: EffectiveSchedule,
    calendar: TradingCalendar,
    exchange_timezone: str,
) -> tuple[Candle, ...]:
    """Canonicalize a run of daily bars to session identity, dropping non-trading days.

    Duplicate canonical session identities (same start/end) are collapsed to the
    first occurrence, so no session is represented twice.
    """
    canonical: list[Candle] = []
    seen: set[tuple[datetime, datetime]] = set()
    for candle in candles:
        session = canonicalize_session_candle(
            candle, effective=effective, calendar=calendar, exchange_timezone=exchange_timezone
        )
        if session is None:
            continue
        identity = (session.start_timestamp, session.end_timestamp)
        if identity in seen:
            continue
        seen.add(identity)
        canonical.append(session)
    return tuple(canonical)
