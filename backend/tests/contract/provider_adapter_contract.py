"""Reusable behavioral assertions for full broker-adapter implementations."""

from __future__ import annotations

from app.adapters.base.broker_adapter import (
    BrokerAdapter,
    HistoricalDataAdapter,
    InstrumentDataAdapter,
    LiveMarketDataAdapter,
)
from app.schemas.market_data import (
    HistoricalRequest,
    HistoricalResult,
    Instrument,
    MarketData,
    ProviderHealth,
    SubscriptionRequest,
)


async def assert_full_adapter_contract(
    adapter: BrokerAdapter, request: SubscriptionRequest
) -> tuple[ProviderHealth, tuple[MarketData, ...]]:
    """Assert a full adapter exposes canonical capabilities without provider knowledge."""
    assert isinstance(adapter, LiveMarketDataAdapter)
    assert isinstance(adapter, HistoricalDataAdapter)
    assert isinstance(adapter, InstrumentDataAdapter)

    await adapter.connect()
    try:
        health = await adapter.get_health()
        events = tuple([event async for event in adapter.stream_market_data(request)])
    finally:
        await adapter.disconnect()

    assert isinstance(health, ProviderHealth)
    assert all(isinstance(event, MarketData) for event in events)
    return health, events


async def assert_historical_instrument_adapter_contract(
    adapter: BrokerAdapter, request: HistoricalRequest
) -> tuple[ProviderHealth, tuple[Instrument, ...], HistoricalResult]:
    """Assert the canonical capabilities required by a REST-only provider adapter."""
    assert isinstance(adapter, HistoricalDataAdapter)
    assert isinstance(adapter, InstrumentDataAdapter)

    await adapter.connect()
    try:
        health = await adapter.get_health()
        instruments = await adapter.load_instruments()
        historical = await adapter.load_historical_data(request)
    finally:
        await adapter.disconnect()

    assert isinstance(health, ProviderHealth)
    assert all(isinstance(instrument, Instrument) for instrument in instruments)
    assert isinstance(historical, HistoricalResult)
    assert all(candle.instrument == request.instrument for candle in historical.candles)
    return health, instruments, historical
