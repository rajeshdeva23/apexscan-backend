"""Composition bridge adapting a Phase-3 provider to the Market Engine's HistoricalSource.

This lives in the composition layer (outside ``app.market_engine``) precisely so the
Market Engine never imports a concrete adapter or broker SDK (ADR-003; docs/03 §3.6).
It wraps any :class:`~app.adapters.base.HistoricalDataAdapter`, advertises the
provider's directly-supported timeframes (configured here, not in generic engine
logic), and translates boundary-safe provider errors into the engine-neutral
:class:`~app.market_engine.historical.source.HistoricalSourceError`.
"""

from __future__ import annotations

from app.adapters.base import HistoricalDataAdapter, ProviderBoundaryError
from app.market_engine.historical.source import HistoricalSource, HistoricalSourceError
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import HistoricalRequest, HistoricalResult

# Directly-supported timeframes for the Dhan provider (docs/05 historical endpoints).
# This capability list is composition configuration — never inside the generic engine.
DHAN_DIRECT_TIMEFRAMES: frozenset[Timeframe] = frozenset(
    {
        Timeframe.minutes(1),
        Timeframe.minutes(5),
        Timeframe.minutes(15),
        Timeframe.minutes(25),
        Timeframe.minutes(60),
        Timeframe.session(),
    }
)


class BrokerHistoricalSource:
    """A :class:`HistoricalSource` backed by a broker-neutral ``HistoricalDataAdapter``."""

    def __init__(
        self, *, adapter: HistoricalDataAdapter, direct_timeframes: frozenset[Timeframe]
    ) -> None:
        """Wire the bridge to a provider adapter and its advertised direct timeframes.

        Args:
            adapter: The Phase-3 historical data adapter to delegate to.
            direct_timeframes: The timeframes the provider fetches directly.
        """
        self._adapter = adapter
        self._direct_timeframes = direct_timeframes

    @property
    def direct_timeframes(self) -> frozenset[Timeframe]:
        """Return the provider's directly-supported timeframes."""
        return self._direct_timeframes

    async def load(self, request: HistoricalRequest) -> HistoricalResult:
        """Delegate to the provider adapter, translating provider errors to the engine's.

        Raises:
            HistoricalSourceError: If the provider raises a boundary error.
        """
        try:
            return await self._adapter.load_historical_data(request)
        except ProviderBoundaryError as error:
            raise HistoricalSourceError(str(error)) from error


def broker_historical_source(adapter: HistoricalDataAdapter) -> HistoricalSource:
    """Build the default Dhan-capability historical source for the given adapter."""
    return BrokerHistoricalSource(adapter=adapter, direct_timeframes=DHAN_DIRECT_TIMEFRAMES)
