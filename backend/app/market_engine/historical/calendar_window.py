"""Pure previous-trading-day resolution over an authoritative calendar window (P4.5A).

"Previous day" means the previous *exchange trading day*, never ``date - 1``.
Resolution reuses the P4.3 :class:`~app.market_engine.session.TradingCalendar`
weekend/holiday semantics unchanged, and adds an explicit authoritative-coverage
bound: outside the known window the calendar cannot prove a date is a trading day,
so resolution fails closed rather than assuming "not a listed holiday ⇒ trading"
(docs/06 §8, §14.2). This module is pure — no wall-clock reads, no host timezone,
no provider I/O — so the same inputs always resolve to the same dates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.market_engine.session import TradingCalendar

_MIN_COUNT = 1


class OutsideCalendarCoverageError(ValueError):
    """Raised when trading-day resolution would leave the authoritative coverage."""


@dataclass(frozen=True, slots=True)
class CalendarCoverage:
    """The inclusive date range over which the trading calendar is authoritative.

    Attributes:
        start_date: The earliest date the calendar can be trusted for (inclusive).
        end_date: The latest date the calendar can be trusted for (inclusive).
    """

    start_date: date
    end_date: date

    def __post_init__(self) -> None:
        """Reject an inverted coverage range, failing fast at construction."""
        if self.end_date < self.start_date:
            raise ValueError("calendar coverage end date must not precede its start date")

    def contains(self, day: date) -> bool:
        """Return whether ``day`` falls within the authoritative coverage (inclusive)."""
        return self.start_date <= day <= self.end_date


class HistoricalCalendarWindow:
    """Resolves previous exchange trading days within an authoritative coverage window."""

    def __init__(self, *, calendar: TradingCalendar, coverage: CalendarCoverage) -> None:
        """Wire the resolver to a trading calendar and its authoritative coverage.

        Args:
            calendar: The P4.3 trading calendar (weekend/holiday semantics reused).
            coverage: The inclusive date range the calendar is authoritative over.
        """
        self._calendar = calendar
        self._coverage = coverage

    @property
    def calendar(self) -> TradingCalendar:
        """Return the underlying trading calendar."""
        return self._calendar

    def previous_trading_day(self, reference: date) -> date:
        """Return the most recent trading day strictly before ``reference``.

        Args:
            reference: The anchor date; it is excluded from the result.

        Returns:
            The nearest earlier exchange trading day.

        Raises:
            OutsideCalendarCoverageError: If the search leaves the authoritative
                coverage before finding a trading day.
        """
        cursor = reference - timedelta(days=1)
        while self._coverage.contains(cursor):
            if self._calendar.is_trading_day(cursor):
                return cursor
            cursor -= timedelta(days=1)
        raise OutsideCalendarCoverageError(
            "no trading day found before the reference within authoritative calendar coverage"
        )

    def previous_trading_days(self, reference: date, count: int) -> tuple[date, ...]:
        """Return the ``count`` trading days before ``reference``, oldest first.

        Args:
            reference: The anchor date; it is excluded from the result.
            count: How many previous trading days to return (must be positive).

        Returns:
            A tuple of trading dates ordered oldest-to-newest.

        Raises:
            ValueError: If ``count`` is not positive.
            OutsideCalendarCoverageError: If the search leaves coverage before
                collecting ``count`` trading days.
        """
        if count < _MIN_COUNT:
            raise ValueError("count must be a positive integer")
        collected: list[date] = []
        cursor = reference
        for _ in range(count):
            cursor = self.previous_trading_day(cursor)
            collected.append(cursor)
        return tuple(reversed(collected))
