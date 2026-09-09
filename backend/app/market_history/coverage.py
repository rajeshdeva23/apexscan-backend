"""Trading-calendar-aware historical coverage (DECOUPLING PHASE F).

Completeness is measured against TRADING DAYS (reusing the existing ``TradingCalendar``), never
naive calendar days — weekends and holidays are never reported missing. No second calendar is
introduced here.
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict

from app.market_engine.session import TradingCalendar

_MAX_CALENDAR_LOOKBACK_DAYS = 3660  # ~10y guard so a misconfigured calendar cannot loop forever


def required_trading_dates(
    calendar: TradingCalendar, *, as_of: date, trading_days: int
) -> tuple[date, ...]:
    """Return the most recent ``trading_days`` trading dates on/before ``as_of`` (ascending).

    Walks backwards skipping weekends/holidays via :meth:`TradingCalendar.is_trading_day`; bounded
    so a broken calendar cannot loop forever.
    """
    if trading_days < 0:
        raise ValueError("trading_days must be non-negative")
    collected: list[date] = []
    cursor = as_of
    for _ in range(_MAX_CALENDAR_LOOKBACK_DAYS):
        if len(collected) >= trading_days:
            break
        if calendar.is_trading_day(cursor):
            collected.append(cursor)
        cursor = cursor - timedelta(days=1)
    return tuple(sorted(collected))


def previous_trading_date(calendar: TradingCalendar, before: date) -> date:
    """Return the latest trading date strictly before ``before`` (Monday resolves to Friday).

    Never uses ``before - 1 day`` naively; holidays and weekends are skipped.
    """
    cursor = before - timedelta(days=1)
    for _ in range(_MAX_CALENDAR_LOOKBACK_DAYS):
        if calendar.is_trading_day(cursor):
            return cursor
        cursor = cursor - timedelta(days=1)
    raise ValueError(f"no trading day within lookback before {before}")


class HistoricalCoverage(BaseModel):
    """Coverage of an instrument's stored history against a required trading-date set."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    instrument_identity: str
    required_dates: tuple[date, ...]
    available_dates: tuple[date, ...]

    @property
    def missing_dates(self) -> tuple[date, ...]:
        """Required trading dates with no stored bar (sorted)."""
        available = set(self.available_dates)
        return tuple(d for d in self.required_dates if d not in available)

    @property
    def is_complete(self) -> bool:
        """Whether every required trading date has a stored bar."""
        return not self.missing_dates

    @property
    def required_count(self) -> int:
        """Number of required trading dates."""
        return len(self.required_dates)

    @property
    def available_count(self) -> int:
        """Number of required trading dates that are stored."""
        return self.required_count - len(self.missing_dates)

    @property
    def earliest_available(self) -> date | None:
        """Earliest stored date within the required range, or None."""
        present = [d for d in self.required_dates if d in set(self.available_dates)]
        return present[0] if present else None

    @property
    def latest_available(self) -> date | None:
        """Latest stored date within the required range, or None."""
        present = [d for d in self.required_dates if d in set(self.available_dates)]
        return present[-1] if present else None


def compute_coverage(
    *,
    instrument_identity: str,
    required_dates: tuple[date, ...],
    stored_dates: tuple[date, ...],
) -> HistoricalCoverage:
    """Build a :class:`HistoricalCoverage` from required trading dates and stored dates."""
    return HistoricalCoverage(
        instrument_identity=instrument_identity,
        required_dates=required_dates,
        available_dates=tuple(sorted(set(stored_dates))),
    )
