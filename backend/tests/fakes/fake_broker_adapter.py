"""Deterministic test-only adapter proving provider contract substitutability."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

from app.adapters.base.broker_adapter import (
    BrokerAdapter,
    HistoricalDataAdapter,
    InstrumentDataAdapter,
    LiveMarketDataAdapter,
)
from app.adapters.base.errors import ProviderContractViolationError
from app.schemas.market_data import (
    Candle,
    HistoricalRequest,
    HistoricalResult,
    Instrument,
    MarketData,
    MarketDataKind,
    ProviderCapability,
    ProviderHealth,
    ProviderStatus,
    SubscriptionRequest,
    Tick,
)

_INSTRUMENT = Instrument(exchange="NSE", symbol="APEX")
_EVENT_TIMESTAMP = datetime(2026, 8, 4, 9, 15, tzinfo=UTC)


def subscription_request() -> SubscriptionRequest:
    """Return a deterministic live request for use by shared contract tests."""
    return SubscriptionRequest(
        instruments=(_INSTRUMENT,),
        data_types=frozenset({MarketDataKind.TICK}),
    )


class FakeBrokerAdapter(
    BrokerAdapter,
    LiveMarketDataAdapter,
    HistoricalDataAdapter,
    InstrumentDataAdapter,
):
    """Network-free fixture implementing the P3.1 provider contract."""

    capabilities = frozenset(
        {
            ProviderCapability.LIVE_MARKET_DATA,
            ProviderCapability.HISTORICAL_DATA,
            ProviderCapability.INSTRUMENTS,
        }
    )

    def __init__(self) -> None:
        self._connected = False

    async def connect(self) -> None:
        """Mark the deterministic test adapter as available."""
        self._connected = True

    async def disconnect(self) -> None:
        """Mark the deterministic test adapter as unavailable."""
        self._connected = False

    async def get_health(self) -> ProviderHealth:
        """Report fixture state using only the canonical health contract."""
        status = ProviderStatus.HEALTHY if self._connected else ProviderStatus.DOWN
        return ProviderHealth(status=status, observed_at=_EVENT_TIMESTAMP)

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        """Yield one canonical event per requested instrument when connected."""
        self._require_connected()
        for instrument in request.instruments:
            yield Tick(
                instrument=instrument,
                event_timestamp=_EVENT_TIMESTAMP,
                last_price=Decimal("101.25"),
                traded_quantity=10,
            )

    async def load_historical_data(self, request: HistoricalRequest) -> HistoricalResult:
        """Return one deterministic canonical candle for contract tests."""
        self._require_connected()
        candle = Candle(
            instrument=request.instrument,
            start_timestamp=request.start_timestamp,
            end_timestamp=request.start_timestamp + request.interval,
            open_price=Decimal("100.00"),
            high_price=Decimal("102.00"),
            low_price=Decimal("99.00"),
            close_price=Decimal("101.50"),
            traded_quantity=25,
        )
        return HistoricalResult(request=request, candles=(candle,))

    async def load_instruments(self) -> tuple[Instrument, ...]:
        """Return the deterministic canonical fixture instrument."""
        self._require_connected()
        return (_INSTRUMENT,)

    def _require_connected(self) -> None:
        if not self._connected:
            raise ProviderContractViolationError()
