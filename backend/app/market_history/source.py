"""Provider-neutral historical data source boundary (DECOUPLING PHASE F).

The core historical package never knows Dhan. A concrete ``DhanHistoricalDataSource`` would live
outside this package and implement :class:`HistoricalDataSource`; Phase F defines only the boundary
and an in-memory reference source for tests. No real provider request, no credentials.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from app.market_history.models import HistoricalDailyBar
from app.schemas.market_data import Instrument


class HistoricalSourceError(RuntimeError):
    """Raised when a historical source cannot supply requested bars (fail-closed)."""


@runtime_checkable
class HistoricalDataSource(Protocol):
    """Fetches raw daily bars for an instrument over an inclusive date range."""

    async def fetch_daily_bars(
        self, instrument: Instrument, start_date: date, end_date: date
    ) -> tuple[HistoricalDailyBar, ...]:
        """Return canonical daily bars for ``instrument`` in ``[start_date, end_date]``."""
        ...


class InMemoryHistoricalDataSource:
    """Reference/test source backed by pre-seeded bars, with optional failure injection."""

    def __init__(self, bars: dict[str, tuple[HistoricalDailyBar, ...]]) -> None:
        self._bars = {identity: tuple(series) for identity, series in bars.items()}
        self.fail_for: set[str] = set()

    async def fetch_daily_bars(
        self, instrument: Instrument, start_date: date, end_date: date
    ) -> tuple[HistoricalDailyBar, ...]:
        """Return seeded bars in range; raise for instruments flagged to fail."""
        identity = f"{instrument.exchange}:{instrument.symbol}"
        if identity in self.fail_for:
            raise HistoricalSourceError(f"source unavailable for {identity}")
        return tuple(
            bar
            for bar in self._bars.get(identity, ())
            if start_date <= bar.trading_date <= end_date
        )
