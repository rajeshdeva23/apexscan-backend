"""Tests for the deterministic market-session layer (docs/06 §7-§8)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest
from pydantic import ValidationError

from app.market_engine.calendar_data import load_nse_cm_2026_dataset
from app.market_engine.context import MarketState
from app.market_engine.session import (
    MarketSessionClassifier,
    SessionSchedule,
    TradingCalendar,
)

# NSE regular schedule (exchange-local IST).
_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)
_TZ = "Asia/Kolkata"


def _classifier(*, holidays: tuple[date, ...] = ()) -> MarketSessionClassifier:
    return MarketSessionClassifier(
        schedule=_SCHEDULE,
        calendar=TradingCalendar(holidays=holidays),
        exchange_timezone=_TZ,
    )


def _utc(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def test_utc_instant_is_interpreted_in_exchange_timezone() -> None:
    # 06:30 UTC == 12:00 IST on a trading Thursday -> mid live session.
    result = _classifier().classify(_utc(2026, 8, 6, 6, 30))
    assert result.market_state is MarketState.LIVE_SESSION
    assert result.trading_date == date(2026, 8, 6)
    assert result.exchange_timezone == _TZ


def test_trading_date_uses_exchange_local_date_across_utc_midnight() -> None:
    # 20:00 UTC on 2026-08-06 == 01:30 IST on 2026-08-07 (before the session).
    result = _classifier().classify(_utc(2026, 8, 6, 20, 0))
    assert result.trading_date == date(2026, 8, 7)
    assert result.market_state is MarketState.MARKET_CLOSED


def test_naive_instant_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _classifier().classify(datetime(2026, 8, 6, 6, 30))  # noqa: DTZ001 (intentionally naive)


def test_weekend_is_a_holiday() -> None:
    saturday = _classifier().classify(_utc(2026, 8, 8, 6, 30))  # Saturday
    sunday = _classifier().classify(_utc(2026, 8, 9, 6, 30))  # Sunday
    assert saturday.market_state is MarketState.HOLIDAY
    assert sunday.market_state is MarketState.HOLIDAY


def test_configured_holiday_is_closed_even_on_a_weekday() -> None:
    classifier = _classifier(holidays=(date(2026, 8, 6),))  # Thursday marked holiday
    result = classifier.classify(_utc(2026, 8, 6, 6, 30))
    assert result.market_state is MarketState.HOLIDAY


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (_utc(2026, 8, 6, 3, 29), MarketState.MARKET_CLOSED),  # 08:59 IST
        (_utc(2026, 8, 6, 3, 30), MarketState.PRE_OPEN),  # 09:00 IST (boundary)
        (_utc(2026, 8, 6, 3, 37), MarketState.PRE_OPEN),  # 09:07 IST
        (_utc(2026, 8, 6, 3, 38), MarketState.OPENING_AUCTION),  # 09:08 IST (boundary)
        (_utc(2026, 8, 6, 3, 44), MarketState.OPENING_AUCTION),  # 09:14 IST
        (_utc(2026, 8, 6, 3, 45), MarketState.LIVE_SESSION),  # 09:15 IST (boundary)
        (_utc(2026, 8, 6, 9, 59), MarketState.LIVE_SESSION),  # 15:29 IST
        (_utc(2026, 8, 6, 10, 0), MarketState.CLOSING_SESSION),  # 15:30 IST (boundary)
        (_utc(2026, 8, 6, 10, 9), MarketState.CLOSING_SESSION),  # 15:39 IST
        (_utc(2026, 8, 6, 10, 10), MarketState.MARKET_CLOSED),  # 15:40 IST (boundary)
        (_utc(2026, 8, 6, 12, 0), MarketState.MARKET_CLOSED),  # 17:30 IST
    ],
)
def test_every_phase_and_boundary_is_classified(instant: datetime, expected: MarketState) -> None:
    assert _classifier().classify(instant).market_state is expected


def test_open_boundary_is_half_open_to_the_microsecond() -> None:
    classifier = _classifier()
    one_before = _utc(2026, 8, 6, 3, 44, 59).replace(microsecond=999999)  # 09:14:59.999999 IST
    at_open = _utc(2026, 8, 6, 3, 45)  # exactly 09:15:00 IST
    one_after = _utc(2026, 8, 6, 3, 45).replace(microsecond=1)
    assert classifier.classify(one_before).market_state is MarketState.OPENING_AUCTION
    assert classifier.classify(at_open).market_state is MarketState.LIVE_SESSION
    assert classifier.classify(one_after).market_state is MarketState.LIVE_SESSION


def test_halt_overrides_the_time_based_phase_on_a_trading_day() -> None:
    classifier = _classifier()
    live = classifier.classify(_utc(2026, 8, 6, 6, 30))
    halted = classifier.classify(_utc(2026, 8, 6, 6, 30), halt_active=True)
    recovered = classifier.classify(_utc(2026, 8, 6, 6, 30), halt_active=False)
    assert live.market_state is MarketState.LIVE_SESSION
    assert halted.market_state is MarketState.EMERGENCY_HALT
    assert recovered.market_state is MarketState.LIVE_SESSION


def test_holiday_takes_precedence_over_a_halt_flag() -> None:
    result = _classifier().classify(_utc(2026, 8, 8, 6, 30), halt_active=True)  # Saturday
    assert result.market_state is MarketState.HOLIDAY


def test_schedule_rejects_non_increasing_boundaries() -> None:
    with pytest.raises(ValidationError, match="strictly increasing"):
        SessionSchedule(
            pre_open_start=time(9, 15),
            opening_auction_start=time(9, 8),
            regular_open=time(9, 0),
            regular_close=time(15, 30),
            closing_end=time(15, 40),
        )


def test_classifier_from_settings_uses_configured_defaults() -> None:
    from app.core.config.settings import Settings

    classifier = MarketSessionClassifier.from_settings(Settings())
    result = classifier.classify(_utc(2026, 8, 6, 6, 30))
    assert result.market_state is MarketState.LIVE_SESSION
    assert result.exchange_timezone == "Asia/Kolkata"


def test_classification_is_deterministic_and_host_timezone_independent() -> None:
    # astimezone(ZoneInfo("Asia/Kolkata")) never reads the host timezone, so the
    # result depends only on the instant and configured exchange timezone.
    classifier = _classifier()
    instant = _utc(2026, 8, 6, 3, 45)
    assert classifier.classify(instant) == classifier.classify(instant)


# --------------------------------------------------------------------------- #
# Coverage-aware classification (ADR-011 live out-of-coverage addendum, §17)
# --------------------------------------------------------------------------- #
_DATASET = load_nse_cm_2026_dataset()


def _covered_classifier() -> MarketSessionClassifier:
    """A classifier over the packaged NSE 2026 dataset calendar + coverage."""
    return MarketSessionClassifier(
        schedule=_SCHEDULE,
        calendar=_DATASET.trading_calendar(),
        exchange_timezone=_TZ,
        coverage=_DATASET.calendar_coverage(),
    )


def _live(year: int, month: int, day: int) -> datetime:
    """06:30 UTC == 12:00 IST — mid-live-session on the same exchange-local date."""
    return _utc(year, month, day, 6, 30)


def test_marketstate_has_calendar_unavailable() -> None:
    assert MarketState.CALENDAR_UNAVAILABLE.value == "calendar_unavailable"
    assert MarketState.CALENDAR_UNAVAILABLE is not MarketState.LIVE_SESSION
    assert MarketState.CALENDAR_UNAVAILABLE is not MarketState.HOLIDAY
    assert MarketState.CALENDAR_UNAVAILABLE is not MarketState.MARKET_CLOSED
    assert MarketState.CALENDAR_UNAVAILABLE is not MarketState.EMERGENCY_HALT


def test_date_before_coverage_is_calendar_unavailable() -> None:
    result = _covered_classifier().classify(_live(2025, 12, 31))
    assert result.market_state is MarketState.CALENDAR_UNAVAILABLE
    assert result.trading_date == date(2025, 12, 31)


def test_date_after_coverage_is_calendar_unavailable() -> None:
    result = _covered_classifier().classify(_live(2027, 1, 1))
    assert result.market_state is MarketState.CALENDAR_UNAVAILABLE
    assert result.trading_date == date(2027, 1, 1)


def test_first_coverage_date_is_authoritative() -> None:
    # 2026-01-01 (Thursday) is inside coverage and not a closed date -> classified,
    # never CALENDAR_UNAVAILABLE (the inclusive lower boundary is authoritative).
    result = _covered_classifier().classify(_live(2026, 1, 1))
    assert result.market_state is MarketState.LIVE_SESSION


def test_last_coverage_date_is_authoritative() -> None:
    # 2026-12-31 (Thursday) is inside coverage (inclusive upper boundary).
    result = _covered_classifier().classify(_live(2026, 12, 31))
    assert result.market_state is MarketState.LIVE_SESSION


def test_cross_midnight_uses_the_instant_not_startup_without_rebuild() -> None:
    # One classifier, no rebuild: 2026-12-31 classifies authoritatively, then the very
    # next exchange-local date 2027-01-01 is CALENDAR_UNAVAILABLE (per-classify check).
    classifier = _covered_classifier()
    last = classifier.classify(_live(2026, 12, 31))
    first_after = classifier.classify(_live(2027, 1, 1))
    assert last.market_state is MarketState.LIVE_SESSION
    assert first_after.market_state is MarketState.CALENDAR_UNAVAILABLE


def test_ordinary_weekday_inside_coverage_is_trading() -> None:
    # 2026-08-06 (Thursday), not a closed date -> normal live phase.
    result = _covered_classifier().classify(_live(2026, 8, 6))
    assert result.market_state is MarketState.LIVE_SESSION


def test_ordinary_sunday_inside_coverage_is_holiday() -> None:
    # 2026-01-04 (Sunday), not an exceptional OPEN -> HOLIDAY (weekend closure).
    assert _covered_classifier().classify(_live(2026, 1, 4)).market_state is MarketState.HOLIDAY


@pytest.mark.parametrize("day", [date(2026, 1, 15), date(2026, 1, 26)])
def test_configured_closed_dates_inside_coverage_are_holidays(day: date) -> None:
    result = _covered_classifier().classify(_live(day.year, day.month, day.day))
    assert result.market_state is MarketState.HOLIDAY


@pytest.mark.parametrize("day", [date(2026, 2, 1), date(2026, 11, 8)])
def test_exceptional_open_sundays_are_trading_capable_not_holiday(day: date) -> None:
    # 2026-02-01 (Budget) & 2026-11-08 (Muhurat) are Sundays promoted to trading by
    # open_sessions; date-level only — intraday phase still from the ordinary schedule.
    result = _covered_classifier().classify(_live(day.year, day.month, day.day))
    assert result.market_state is not MarketState.HOLIDAY
    assert result.market_state is MarketState.LIVE_SESSION


def test_no_settings_holidays_fallback_outside_coverage() -> None:
    # 2027-01-01 is an ordinary Friday; without coverage a weekday-trading rule would
    # infer LIVE_SESSION. Out of coverage it MUST be CALENDAR_UNAVAILABLE, never inferred.
    assert _covered_classifier().classify(_live(2027, 1, 1)).market_state is (
        MarketState.CALENDAR_UNAVAILABLE
    )


def test_coverage_none_preserves_legacy_no_check_behaviour() -> None:
    # The default (coverage=None) performs no coverage check: an out-of-2026 weekday
    # classifies via the calendar exactly as before this addendum (backward compat).
    legacy = MarketSessionClassifier(
        schedule=_SCHEDULE, calendar=TradingCalendar(), exchange_timezone=_TZ
    )
    assert legacy.classify(_live(2027, 1, 1)).market_state is MarketState.LIVE_SESSION
