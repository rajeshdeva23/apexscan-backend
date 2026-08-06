"""Broker-neutral adapter capabilities for the Data Provider boundary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from app.schemas.market_data import (
    HistoricalRequest,
    HistoricalResult,
    Instrument,
    MarketData,
    ProviderCapability,
    ProviderHealth,
    SubscriptionRequest,
)


class BrokerAdapter(ABC):
    """Lifecycle and health contract shared by all provider adapters."""

    capabilities: frozenset[ProviderCapability]

    @abstractmethod
    async def connect(self) -> None:
        """Initialize the adapter's provider-side state."""
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        """Release the adapter's provider-side state."""
        raise NotImplementedError

    @abstractmethod
    async def get_health(self) -> ProviderHealth:
        """Return a canonical provider-health observation."""
        raise NotImplementedError


@runtime_checkable
class LiveMarketDataAdapter(Protocol):
    """Capability for streaming canonical live market data."""

    def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        """Yield normalized events matching the requested canonical subscription."""


@runtime_checkable
class HistoricalDataAdapter(Protocol):
    """Capability for loading canonical historical candle data."""

    async def load_historical_data(self, request: HistoricalRequest) -> HistoricalResult:
        """Return normalized historical data for one canonical request."""


@runtime_checkable
class InstrumentDataAdapter(Protocol):
    """Capability for loading canonical instrument identities."""

    async def load_instruments(self) -> tuple[Instrument, ...]:
        """Return canonical instrument identities known to the provider."""
