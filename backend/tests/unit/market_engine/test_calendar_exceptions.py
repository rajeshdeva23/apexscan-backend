"""Calendar exception model: OPEN sessions, closed dates, and per-date hours.

Covers the ADR-011 addendum classification precedence (M5), the OPEN/CLOSED
conflict rule (M4), and the broker-neutral session-hours value objects
(``TradingInterval``, ``TradingSessionOverride``, ``EffectiveSchedule``). All dates
are synthetic; no real NSE/Muhurat/BCP dates are used.
"""

from __future__ import annotations

from datetime import date, time

import pytest

from app.market_engine.session import (
    EffectiveSchedule,
    SessionSchedule,
    TradingCalendar,
    TradingInterval,
    TradingSessionOverride,
)

# 2026-08-06 Thu, 08-07 Fri, 08-08 Sat, 08-09 Sun, 08-10 Mon.
_THURSDAY = date(2026, 8, 6)
_FRIDAY = date(2026, 8, 7)
_SATURDAY = date(2026, 8, 8)
_SUNDAY = date(2026, 8, 9)
_MONDAY = date(2026, 8, 10)

_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)


# --------------------------------------------------------------------------- #
# Classification precedence (M5) and conflict (M4)
# --------------------------------------------------------------------------- #
def test_ordinary_weekday_is_a_trading_day() -> None:
    assert TradingCalendar().is_trading_day(_THURSDAY) is True


def test_weekend_is_closed() -> None:
    calendar = TradingCalendar()
    assert calendar.is_trading_day(_SATURDAY) is False
    assert calendar.is_trading_day(_SUNDAY) is False


def test_explicit_closed_weekday_is_closed() -> None:
    calendar = TradingCalendar(closed_dates=(_THURSDAY,))
    assert calendar.is_trading_day(_THURSDAY) is False


def test_explicit_open_saturday_is_a_trading_day() -> None:
    calendar = TradingCalendar(open_sessions=(_SATURDAY,))
    assert calendar.is_trading_day(_SATURDAY) is True


def test_explicit_open_sunday_is_a_trading_day() -> None:
    calendar = TradingCalendar(open_sessions=(_SUNDAY,))
    assert calendar.is_trading_day(_SUNDAY) is True


def test_open_and_closed_conflict_is_rejected() -> None:
    with pytest.raises(ValueError, match="both open and closed"):
        TradingCalendar(closed_dates=(_SATURDAY,), open_sessions=(_SATURDAY,))


def test_holiday_alias_conflict_with_open_is_rejected() -> None:
    with pytest.raises(ValueError, match="both open and closed"):
        TradingCalendar(holidays=(_THURSDAY,), open_sessions=(_THURSDAY,))


# --------------------------------------------------------------------------- #
# Backward compatibility — empty open_sessions preserves legacy behaviour
# --------------------------------------------------------------------------- #
def test_empty_open_sessions_preserves_legacy_holiday_semantics() -> None:
    calendar = TradingCalendar(holidays=(_THURSDAY,))
    assert calendar.is_trading_day(_THURSDAY) is False
    assert calendar.is_trading_day(_FRIDAY) is True
    assert calendar.is_trading_day(_SATURDAY) is False
    assert calendar.open_sessions == frozenset()
    assert calendar.holidays == frozenset({_THURSDAY})
    assert calendar.closed_dates == frozenset({_THURSDAY})


def test_holidays_and_closed_dates_are_unioned() -> None:
    calendar = TradingCalendar(holidays=(_THURSDAY,), closed_dates=(_FRIDAY,))
    assert calendar.closed_dates == frozenset({_THURSDAY, _FRIDAY})


# --------------------------------------------------------------------------- #
# TradingInterval / SessionSchedule.bounds
# --------------------------------------------------------------------------- #
def test_session_schedule_bounds_property() -> None:
    bounds = _SCHEDULE.bounds
    assert bounds == TradingInterval(start=time(9, 15), end=time(15, 30))


def test_trading_interval_rejects_non_increasing() -> None:
    with pytest.raises(ValueError, match="start < end"):
        TradingInterval(start=time(15, 30), end=time(9, 15))


def test_trading_interval_rejects_equal() -> None:
    with pytest.raises(ValueError, match="start < end"):
        TradingInterval(start=time(9, 15), end=time(9, 15))


# --------------------------------------------------------------------------- #
# TradingSessionOverride
# --------------------------------------------------------------------------- #
def test_override_exposes_live_intervals() -> None:
    override = TradingSessionOverride.continuous(
        trading_date=_SATURDAY, start=time(10, 0), end=time(14, 0)
    )
    assert override.live_intervals == (TradingInterval(start=time(10, 0), end=time(14, 0)),)


def test_override_rejects_non_increasing_hours() -> None:
    with pytest.raises(ValueError, match="start < end"):
        TradingSessionOverride.continuous(
            trading_date=_SATURDAY, start=time(14, 0), end=time(10, 0)
        )


def test_override_rejects_equal_hours() -> None:
    with pytest.raises(ValueError, match="start < end"):
        TradingSessionOverride.continuous(
            trading_date=_SATURDAY, start=time(10, 0), end=time(10, 0)
        )


# --------------------------------------------------------------------------- #
# EffectiveSchedule (M10 / MI16)
# --------------------------------------------------------------------------- #
def test_effective_schedule_default_for_ordinary_date() -> None:
    effective = EffectiveSchedule(default=_SCHEDULE)
    assert effective.has_override(_THURSDAY) is False
    assert effective.intervals_for(_THURSDAY) == (_SCHEDULE.bounds,)


def test_effective_schedule_uses_override_intervals() -> None:
    override = TradingSessionOverride.continuous(
        trading_date=_SATURDAY, start=time(10, 0), end=time(14, 0)
    )
    effective = EffectiveSchedule(default=_SCHEDULE, overrides=(override,))
    assert effective.has_override(_SATURDAY) is True
    assert effective.intervals_for(_SATURDAY) == override.live_intervals
    # No leakage to adjacent ordinary dates.
    assert effective.intervals_for(_FRIDAY) == (_SCHEDULE.bounds,)


def test_effective_schedule_rejects_duplicate_override_date() -> None:
    first = TradingSessionOverride.continuous(
        trading_date=_SATURDAY, start=time(10, 0), end=time(14, 0)
    )
    second = TradingSessionOverride.continuous(
        trading_date=_SATURDAY, start=time(11, 0), end=time(13, 0)
    )
    with pytest.raises(ValueError, match="duplicate session override"):
        EffectiveSchedule(default=_SCHEDULE, overrides=(first, second))
