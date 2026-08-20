"""Canonical contract tests for ProviderSessionOhlc and Tick.session_ohlc (ADR-008, P4.6A)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.market_data import Instrument, ProviderSessionOhlc, Tick

_EVENT_TIME = datetime(2026, 8, 10, 6, 30, tzinfo=UTC)


def _instrument() -> Instrument:
    return Instrument(exchange="NSE", symbol="RELIANCE")


def _ohlc(
    *,
    open_price: str = "100",
    high_price: str = "101.5",
    low_price: str = "99.5",
    close: str = "100.25",
) -> ProviderSessionOhlc:
    return ProviderSessionOhlc(
        open_price=Decimal(open_price),
        high_price=Decimal(high_price),
        low_price=Decimal(low_price),
        close_price=Decimal(close),
    )


def test_valid_aggregate_constructs() -> None:
    ohlc = _ohlc()
    assert ohlc.open_price == Decimal("100")
    assert ohlc.high_price == Decimal("101.5")
    assert ohlc.low_price == Decimal("99.5")
    assert ohlc.close_price == Decimal("100.25")


def test_aggregate_is_immutable() -> None:
    ohlc = _ohlc()
    with pytest.raises(ValidationError):
        ohlc.high_price = Decimal("200")  # type: ignore[misc]


def test_high_below_low_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _ohlc(high_price="98", low_price="99")


def test_open_below_low_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _ohlc(open_price="98", low_price="99.5")


def test_open_above_high_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _ohlc(open_price="102", high_price="101.5")


def test_close_outside_the_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _ohlc(close="102")


@pytest.mark.parametrize("field", ["open_price", "high_price", "low_price", "close_price"])
def test_nonpositive_price_is_rejected(field: str) -> None:
    values = {
        "open_price": Decimal("100"),
        "high_price": Decimal("101.5"),
        "low_price": Decimal("99.5"),
        "close_price": Decimal("100.25"),
    }
    values[field] = Decimal("0")
    with pytest.raises(ValidationError):
        ProviderSessionOhlc(**values)


def test_tick_accepts_session_ohlc() -> None:
    tick = Tick(
        instrument=_instrument(),
        event_timestamp=_EVENT_TIME,
        last_price=Decimal("100"),
        session_ohlc=_ohlc(),
    )
    assert tick.session_ohlc == _ohlc()


def test_tick_without_session_ohlc_defaults_to_none() -> None:
    tick = Tick(instrument=_instrument(), event_timestamp=_EVENT_TIME, last_price=Decimal("100"))
    assert tick.session_ohlc is None


def test_serialization_round_trip_preserves_session_ohlc() -> None:
    tick = Tick(
        instrument=_instrument(),
        event_timestamp=_EVENT_TIME,
        last_price=Decimal("100"),
        session_ohlc=_ohlc(),
    )
    restored = Tick.model_validate(tick.model_dump())
    assert restored == tick
    assert restored.session_ohlc == _ohlc()


def test_no_provider_specific_field_names_in_serialization() -> None:
    dumped = _ohlc().model_dump()
    assert set(dumped) == {"open_price", "high_price", "low_price", "close_price"}
    for forbidden in ("day_open", "day_high", "day_low", "day_close"):
        assert forbidden not in dumped
