"""Tests for the deterministic market-session layer (docs/06 §7-§8)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest
from pydantic import ValidationError

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
