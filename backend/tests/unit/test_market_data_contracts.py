"""Tests for broker-neutral Data Provider contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib import import_module
from typing import Any

import pytest
from pydantic import ValidationError


def _contracts() -> Any:
    """Return the canonical market-data contracts or fail with the intended requirement."""
    try:
        return import_module("app.schemas.market_data")
    except ModuleNotFoundError:
        pytest.fail("P3.1 must provide canonical broker-neutral market-data contracts")


def _utc_datetime() -> datetime:
    """Return a deterministic timezone-aware market event timestamp."""
    return datetime(2026, 8, 4, 9, 15, tzinfo=UTC)


def _instrument() -> Any:
    """Create the minimum broker-neutral identity used by market-data contracts."""
    contracts = _contracts()
    return contracts.Instrument(exchange="NSE", symbol="APEX")


def _candle() -> Any:
    """Create one valid canonical candle for historical contract assertions."""
    contracts = _contracts()
    start = _utc_datetime()
    return contracts.Candle(
        instrument=_instrument(),
        start_timestamp=start,
        end_timestamp=start + timedelta(minutes=1),
        open_price=Decimal("100.00"),
        high_price=Decimal("102.00"),
        low_price=Decimal("99.00"),
        close_price=Decimal("101.50"),
        traded_quantity=25,
    )


def test_canonical_contracts_are_immutable_and_broker_neutral() -> None:
    """Canonical outputs use typed ApexScan values and reject mutation."""
    contracts = _contracts()
    instrument = _instrument()
    tick = contracts.Tick(
        instrument=instrument,
        event_timestamp=_utc_datetime(),
        last_price=Decimal("101.25"),
        traded_quantity=10,
    )

    assert instrument.exchange == "NSE"
    assert instrument.symbol == "APEX"
    assert tick.model_dump(mode="json") == {
        "instrument": {
            "exchange": "NSE",
            "market_segment": "equity",
            "symbol": "APEX",
            "instrument_class": "cash",
            "underlying": None,
            "display_name": None,
            "listing_type": None,
            "series": None,
            "expiry": None,
            "strike_price": None,
            "option_type": None,
        },
        "event_timestamp": "2026-08-04T09:15:00Z",
        "last_price": "101.25",
        "traded_quantity": 10,
        "session_cumulative_volume": None,
        "session_ohlc": None,
    }
    assert "dhan" not in repr(tick).lower()

    with pytest.raises(ValidationError):
        tick.last_price = Decimal("102.00")


def test_canonical_contracts_reject_ambiguous_timestamps_and_invalid_market_values() -> None:
    """Canonical data never accepts a naive clock value or fabricated prices."""
    contracts = _contracts()

    with pytest.raises(ValidationError, match="timezone-aware"):
        contracts.Tick(
            instrument=_instrument(),
            event_timestamp=datetime(2026, 8, 4, 9, 15),
            last_price=Decimal("101.25"),
            traded_quantity=10,
        )

    with pytest.raises(ValidationError):
        contracts.Quote(
            instrument=_instrument(),
            event_timestamp=_utc_datetime(),
            bid_price=Decimal("0"),
            ask_price=Decimal("101.50"),
            bid_quantity=1,
            ask_quantity=1,
        )

    with pytest.raises(ValidationError):
        contracts.Candle(
            instrument=_instrument(),
            start_timestamp=_utc_datetime(),
            end_timestamp=_utc_datetime() + timedelta(minutes=1),
            open_price=Decimal("100.00"),
            high_price=Decimal("99.00"),
            low_price=Decimal("98.00"),
            close_price=Decimal("99.00"),
            traded_quantity=0,
        )

    depth = contracts.DepthSnapshot(
        instrument=_instrument(),
        event_timestamp=_utc_datetime(),
        bids=(contracts.DepthLevel(price=Decimal("101.00"), quantity=3),),
        asks=(contracts.DepthLevel(price=Decimal("101.50"), quantity=4),),
    )
    assert depth.bids[0].quantity == 3

    with pytest.raises(ValidationError):
        contracts.DepthSnapshot(
            instrument=_instrument(),
            event_timestamp=_utc_datetime(),
            bids=(),
            asks=(contracts.DepthLevel(price=Decimal("101.50"), quantity=4),),
        )


def test_capability_contracts_share_explicit_instrument_and_time_semantics() -> None:
    """Live, historical, subscription, and health contracts use only canonical concepts."""
    contracts = _contracts()
    instrument = _instrument()
    candle = _candle()
    request = contracts.HistoricalRequest(
        instrument=instrument,
        start_timestamp=_utc_datetime() - timedelta(days=1),
        end_timestamp=_utc_datetime(),
        interval=timedelta(minutes=1),
    )
    result = contracts.HistoricalResult(request=request, candles=(candle,))
    subscription = contracts.SubscriptionRequest(
        instruments=(instrument,),
        data_types=frozenset({contracts.MarketDataKind.TICK, contracts.MarketDataKind.QUOTE}),
    )
    health = contracts.ProviderHealth(
        status=contracts.ProviderStatus.HEALTHY,
        observed_at=_utc_datetime(),
    )

    assert result.candles == (candle,)
    assert subscription.instruments == (instrument,)
    assert health.status is contracts.ProviderStatus.HEALTHY

    with pytest.raises(ValidationError):
        contracts.HistoricalRequest(
            instrument=instrument,
            start_timestamp=_utc_datetime(),
            end_timestamp=_utc_datetime(),
            interval=timedelta(minutes=1),
        )

    with pytest.raises(ValidationError):
        contracts.SubscriptionRequest(instruments=(instrument,), data_types=frozenset())

    with pytest.raises(ValidationError):
        contracts.SubscriptionRequest(
            instruments=(instrument,),
            data_types=frozenset({"unsupported-market-data-kind"}),
        )


def test_market_reference_is_immutable_broker_neutral_and_positive() -> None:
    """A session reference carries a provider-independent, strictly-positive previous close."""
    contracts = _contracts()
    instrument = _instrument()

    reference = contracts.MarketReference(instrument=instrument, previous_close=Decimal("100.50"))
    assert reference.instrument == instrument
    assert reference.previous_close == Decimal("100.50")
    assert contracts.MarketReference in contracts.MarketData.__args__

    with pytest.raises(ValidationError):
        reference.previous_close = Decimal("101")  # type: ignore[misc]

    with pytest.raises(ValidationError):
        contracts.MarketReference(instrument=instrument, previous_close=Decimal("0"))

    with pytest.raises(ValidationError):
        contracts.MarketReference(instrument=instrument, previous_close=Decimal("-1"))

    with pytest.raises(ValidationError):
        contracts.MarketReference(
            instrument=instrument, previous_close=Decimal("100"), open_interest=1
        )
