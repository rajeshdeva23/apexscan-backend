"""Broker-neutral adapter capabilities for the Data Provider boundary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from app.schemas.market_data import (
    HistoricalRequest,
    HistoricalResult,
    Instrument,
    MarketData,
    ProviderCapability,
    ProviderHealth,
    SessionStatisticsObservation,
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


@runtime_checkable
class SessionStatisticsSource(Protocol):
    """Capability for loading canonical current-session statistics (ADR-009 D2).

    Broker-neutral and batch-capable: given canonical instruments plus the caller's
    exchange ``trading_date`` and ``observed_at`` (the composition-supplied instant the
    snapshot became known — no wall-clock read here), it returns immutable canonical
    :class:`SessionStatisticsObservation` values. It performs no MarketContext mutation
    and knows nothing of strategies. Presence of an observation implies no authority
    (ADR-009 D6); verification is a separate concern.
    """

    async def load_session_statistics(
        self,
        instruments: Sequence[Instrument],
        *,
        trading_date: date,
        observed_at: datetime,
    ) -> tuple[SessionStatisticsObservation, ...]:
        """Return canonical current-session observations for the given instruments."""
