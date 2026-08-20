"""Canonical contract tests for SessionStatisticsObservation (ADR-009 D1; P4.6E1)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.market_data import Instrument, ProviderSessionOhlc, SessionStatisticsObservation

_DATE = date(2026, 8, 11)
_OBSERVED_AT = datetime(2026, 8, 11, 6, 30, tzinfo=UTC)


def _instrument() -> Instrument:
    return Instrument(exchange="NSE", symbol="RELIANCE")


def _ohlc(
    *, open_: str = "100", high: str = "105", low: str = "98", close: str = "101"
) -> ProviderSessionOhlc:
    return ProviderSessionOhlc(
        open_price=Decimal(open_),
        high_price=Decimal(high),
        low_price=Decimal(low),
        close_price=Decimal(close),
    )


def _observation(
    *, trading_date: date = _DATE, observed_at: datetime = _OBSERVED_AT
) -> SessionStatisticsObservation:
    return SessionStatisticsObservation(
        instrument=_instrument(),
        trading_date=trading_date,
        observed_at=observed_at,
        session_ohlc=_ohlc(),
    )


def test_valid_construction() -> None:
    obs = _observation()
    assert obs.instrument == _instrument()
    assert obs.trading_date == _DATE
    assert obs.observed_at == _OBSERVED_AT
    assert obs.session_ohlc == _ohlc()


def test_observation_is_immutable() -> None:
    obs = _observation()
    with pytest.raises(ValidationError):
        obs.trading_date = date(2026, 8, 12)  # type: ignore[misc]


def test_naive_observed_at_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _observation(observed_at=datetime(2026, 8, 11, 6, 30))  # naive


def test_aware_non_utc_observed_at_is_normalised_to_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    obs = _observation(observed_at=datetime(2026, 8, 11, 12, 0, tzinfo=ist))
    assert obs.observed_at.tzinfo is UTC
    assert obs.observed_at == datetime(2026, 8, 11, 6, 30, tzinfo=UTC)


def test_provider_session_ohlc_retained_exactly() -> None:
    ohlc = _ohlc(open_="100", high="108", low="95", close="102")
    obs = SessionStatisticsObservation(
        instrument=_instrument(), trading_date=_DATE, observed_at=_OBSERVED_AT, session_ohlc=ohlc
    )
    assert obs.session_ohlc is ohlc


def test_canonical_instrument_retained_exactly() -> None:
    instrument = Instrument(exchange="NSE", symbol="TCS")
    obs = SessionStatisticsObservation(
        instrument=instrument, trading_date=_DATE, observed_at=_OBSERVED_AT, session_ohlc=_ohlc()
    )
    assert obs.instrument == instrument


def test_explicit_trading_date_preserved_and_not_derived_from_observed_at() -> None:
    # observed_at is 2026-08-11 but trading_date is a different, explicitly supplied day.
    obs = _observation(trading_date=date(2026, 8, 7), observed_at=_OBSERVED_AT)
    assert obs.trading_date == date(2026, 8, 7)


def test_deterministic_equality() -> None:
    assert _observation() == _observation()


def test_deterministic_model_dump_shape() -> None:
    dumped = _observation().model_dump()
    assert set(dumped) == {"instrument", "trading_date", "observed_at", "session_ohlc"}


def test_json_round_trip_preserves_equality() -> None:
    obs = _observation()
    assert SessionStatisticsObservation.model_validate_json(obs.model_dump_json()) == obs


def test_extra_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SessionStatisticsObservation(
            instrument=_instrument(),
            trading_date=_DATE,
            observed_at=_OBSERVED_AT,
            session_ohlc=_ohlc(),
            quality="authoritative",  # type: ignore[call-arg]
        )


def test_no_authority_or_freshness_fields_exist() -> None:
    fields = set(SessionStatisticsObservation.model_fields)
    assert fields == {"instrument", "trading_date", "observed_at", "session_ohlc"}
    for forbidden in ("quality", "is_authoritative", "verified", "max_age", "expires_at", "ttl"):
        assert forbidden not in fields


def test_no_provider_specific_fields_in_serialization() -> None:
    dumped = _observation().model_dump()
    for forbidden in ("provider", "dhan_security_id", "exchange_segment_code", "response_code"):
        assert forbidden not in dumped


def test_structurally_invalid_nested_ohlc_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SessionStatisticsObservation(
            instrument=_instrument(),
            trading_date=_DATE,
            observed_at=_OBSERVED_AT,
            session_ohlc=ProviderSessionOhlc(
                open_price=Decimal("100"),
                high_price=Decimal("98"),  # high < low
                low_price=Decimal("99"),
                close_price=Decimal("100"),
            ),
        )


def test_missing_session_ohlc_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SessionStatisticsObservation(  # type: ignore[call-arg]
            instrument=_instrument(), trading_date=_DATE, observed_at=_OBSERVED_AT
        )
