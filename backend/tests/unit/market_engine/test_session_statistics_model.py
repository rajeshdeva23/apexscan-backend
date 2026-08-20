"""Contract tests for the SessionStatistics fact and its quality model (P4.6B; ADR-008)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.market_engine.context import SessionStatistics, SessionStatisticsQuality
from app.market_engine.state import InstrumentState
from app.schemas.market_data import Instrument

_DATE = date(2026, 8, 10)
_AS_OF = datetime(2026, 8, 10, 9, 20, tzinfo=UTC)
_AUTH = SessionStatisticsQuality.AUTHORITATIVE
_NA = SessionStatisticsQuality.UNAVAILABLE


def _authoritative(*, open_: str = "100", high: str = "105", low: str = "98") -> SessionStatistics:
    return SessionStatistics(
        trading_date=_DATE,
        open_price=Decimal(open_),
        high_price=Decimal(high),
        low_price=Decimal(low),
        quality=_AUTH,
        as_of=_AS_OF,
    )


def test_valid_authoritative_statistics_construct() -> None:
    stats = _authoritative()
    assert stats.quality is _AUTH
    assert (stats.open_price, stats.high_price, stats.low_price) == (
        Decimal("100"),
        Decimal("105"),
        Decimal("98"),
    )


def test_unavailable_statistics_carry_no_prices() -> None:
    stats = SessionStatistics(trading_date=_DATE, quality=_NA, as_of=_AS_OF)
    assert stats.open_price is None and stats.high_price is None and stats.low_price is None


def test_unavailable_with_a_price_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SessionStatistics(trading_date=_DATE, open_price=Decimal("100"), quality=_NA, as_of=_AS_OF)


@pytest.mark.parametrize("missing", ["open_price", "high_price", "low_price"])
def test_authoritative_missing_a_price_is_rejected(missing: str) -> None:
    values = {
        "open_price": Decimal("100"),
        "high_price": Decimal("105"),
        "low_price": Decimal("98"),
    }
    del values[missing]
    with pytest.raises(ValidationError):
        SessionStatistics(trading_date=_DATE, quality=_AUTH, as_of=_AS_OF, **values)


def test_high_below_low_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _authoritative(high="97", low="98")


def test_open_outside_the_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _authoritative(open_="110", high="105", low="98")


def test_nonpositive_price_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _authoritative(low="0")


def test_naive_as_of_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SessionStatistics(
            trading_date=_DATE,
            open_price=Decimal("100"),
            high_price=Decimal("105"),
            low_price=Decimal("98"),
            quality=_AUTH,
            as_of=datetime(2026, 8, 10, 9, 20),  # naive
        )


def test_aware_non_utc_as_of_is_normalised_to_utc() -> None:
    stats = _authoritative()
    assert stats.as_of.tzinfo is UTC


def test_statistics_are_immutable() -> None:
    stats = _authoritative()
    with pytest.raises(ValidationError):
        stats.high_price = Decimal("999")  # type: ignore[misc]


def test_instrument_state_owns_session_statistics_defaulting_none() -> None:
    state = InstrumentState(instrument=Instrument(exchange="NSE", symbol="RELIANCE"))
    assert state.session_statistics is None
    state.session_statistics = _authoritative()
    assert state.session_statistics == _authoritative()
