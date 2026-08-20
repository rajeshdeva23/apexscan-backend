"""Pure previous-trading-day resolution over authoritative coverage (P4.5A; §32)."""

from __future__ import annotations

from datetime import date

import pytest

from app.market_engine.historical.calendar_window import (
    CalendarCoverage,
    HistoricalCalendarWindow,
    OutsideCalendarCoverageError,
)
from app.market_engine.session import TradingCalendar

_COVERAGE = CalendarCoverage(start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))


def _window(
    *, holidays: tuple[date, ...] = (), coverage: CalendarCoverage = _COVERAGE
) -> HistoricalCalendarWindow:
    return HistoricalCalendarWindow(calendar=TradingCalendar(holidays=holidays), coverage=coverage)


def test_previous_trading_day_of_monday_is_friday() -> None:
    # 2026-08-10 is a Monday; the previous trading day is Friday 2026-08-07.
    assert _window().previous_trading_day(date(2026, 8, 10)) == date(2026, 8, 7)


def test_previous_trading_day_skips_a_holiday() -> None:
    # Monday 2026-08-10 is a holiday; from Tuesday the previous trading day is Friday.
    window = _window(holidays=(date(2026, 8, 10),))
    assert window.previous_trading_day(date(2026, 8, 11)) == date(2026, 8, 7)


def test_previous_trading_day_skips_consecutive_holidays_and_weekend() -> None:
    # Thu/Fri holidays before a weekend: from Monday, step back to Wednesday.
    window = _window(holidays=(date(2026, 8, 6), date(2026, 8, 7)))
    assert window.previous_trading_day(date(2026, 8, 10)) == date(2026, 8, 5)


def test_previous_n_returns_exact_count_oldest_first() -> None:
    days = _window().previous_trading_days(date(2026, 8, 10), 3)
    assert days == (date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7))


def test_previous_n_count_zero_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        _window().previous_trading_days(date(2026, 8, 10), 0)


def test_previous_n_negative_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        _window().previous_trading_days(date(2026, 8, 10), -2)


def test_resolution_outside_coverage_fails_closed() -> None:
    coverage = CalendarCoverage(start_date=date(2026, 8, 10), end_date=date(2026, 8, 31))
    window = _window(coverage=coverage)
    # 2026-08-10 is a Monday; the previous trading day (Fri 08-07) is before coverage.
    with pytest.raises(OutsideCalendarCoverageError):
        window.previous_trading_day(date(2026, 8, 10))


def test_coverage_start_boundary_is_inclusive() -> None:
    # Coverage starts Fri 2026-08-07; from Mon 08-10 that Friday is resolvable.
    coverage = CalendarCoverage(start_date=date(2026, 8, 7), end_date=date(2026, 8, 31))
    window = _window(coverage=coverage)
    assert window.previous_trading_day(date(2026, 8, 10)) == date(2026, 8, 7)


def test_coverage_end_boundary_is_inclusive() -> None:
    # Coverage ends Fri 2026-08-07; from Sat 08-08 the inclusive end date resolves.
    coverage = CalendarCoverage(start_date=date(2026, 8, 1), end_date=date(2026, 8, 7))
    window = _window(coverage=coverage)
    assert window.previous_trading_day(date(2026, 8, 8)) == date(2026, 8, 7)


def test_inverted_coverage_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not precede"):
        CalendarCoverage(start_date=date(2026, 8, 31), end_date=date(2026, 8, 1))


# --------------------------------------------------------------------------- #
# Exceptional OPEN sessions (ADR-011 addendum M14)
# --------------------------------------------------------------------------- #
_SATURDAY = date(2026, 8, 8)
_SUNDAY = date(2026, 8, 9)


def _open_window(
    *, open_sessions: tuple[date, ...], coverage: CalendarCoverage = _COVERAGE
) -> HistoricalCalendarWindow:
    return HistoricalCalendarWindow(
        calendar=TradingCalendar(open_sessions=open_sessions), coverage=coverage
    )


def test_previous_trading_day_includes_weekend_open() -> None:
    # Saturday 2026-08-08 is OPEN; from Monday it is the previous trading day.
    window = _open_window(open_sessions=(_SATURDAY,))
    assert window.previous_trading_day(date(2026, 8, 10)) == _SATURDAY


def test_previous_trading_days_include_open_oldest_first() -> None:
    # From Monday: Sat (open), Fri, Thu — ordered oldest to newest.
    window = _open_window(open_sessions=(_SATURDAY,))
    days = window.previous_trading_days(date(2026, 8, 10), 3)
    assert days == (date(2026, 8, 6), date(2026, 8, 7), _SATURDAY)


def test_explicit_closed_weekday_is_skipped() -> None:
    # Friday 08-07 is closed via closed_dates; from Monday the walk skips the
    # weekend and the closed Friday, landing on Thursday 08-06.
    calendar = TradingCalendar(closed_dates=(date(2026, 8, 7),))
    window = HistoricalCalendarWindow(calendar=calendar, coverage=_COVERAGE)
    assert window.previous_trading_day(date(2026, 8, 10)) == date(2026, 8, 6)


def test_open_override_outside_coverage_does_not_extend_authority() -> None:
    # Saturday 08-08 is OPEN but coverage starts Monday 08-10; walking back from
    # Monday leaves coverage before reaching it, so the OPEN date is never authoritative.
    coverage = CalendarCoverage(start_date=date(2026, 8, 10), end_date=date(2026, 8, 31))
    window = _open_window(open_sessions=(_SATURDAY,), coverage=coverage)
    with pytest.raises(OutsideCalendarCoverageError):
        window.previous_trading_day(date(2026, 8, 10))
