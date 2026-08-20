"""Synthetic, network-free provider for the offline validation harness.

Feeds the *real* market-engine / strategy / scanner pipeline a deterministic
208-instrument NSE cash-equity universe so a genuine Narrow CPR snapshot can be
produced with no broker credentials and no network. Every candle satisfies the
pivot identity ``H + L + C = 300`` (pivot = 100), so ``cpr_width_pct = |close - 100|``
and the ascending-width rank order equals the instrument index order.

This is a *validation* fixture, not production data: it is only reachable through
the offline harness app (``app.offline``), never the production composition root.
A scattered set of instruments is left without history to exercise the honest
partial-universe path end-to-end (205 ranked / 3 unavailable).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

from app.adapters.dhan.models import DhanCashEquityLiveUniverse, DhanInstrumentReference
from app.schemas.market_data import (
    Candle,
    HistoricalRequest,
    HistoricalResult,
    Instrument,
    MarketData,
    ProviderHealth,
    ProviderStatus,
    SubscriptionRequest,
    Tick,
)

# Deterministic synthetic universe: SYM000..SYM207.
UNIVERSE_SIZE = 208
SYMBOLS: tuple[str, ...] = tuple(f"SYM{index:03d}" for index in range(UNIVERSE_SIZE))
_INDEX: dict[str, int] = {symbol: index for index, symbol in enumerate(SYMBOLS)}

# Scattered instruments left without history — the honest 205/3 partial split.
DEFAULT_UNAVAILABLE: frozenset[str] = frozenset({"SYM003", "SYM100", "SYM204"})

# 2026-08-06 06:00 UTC == 11:30 IST Thursday: a regular session within the packaged
# NSE 2026 calendar, so warmup resolves the previous completed session (2026-08-05).
REFERENCE_INSTANT = datetime(2026, 8, 6, 6, 0, tzinfo=UTC)


def _instrument(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _close_for(symbol: str) -> Decimal:
    """Close price giving ``cpr_width_pct = (index + 1) / 10`` under H+L+C=300."""
    return Decimal("100") + (Decimal(_INDEX[symbol] + 1) / Decimal(10))


class OfflineFixtureProvider:
    """A synthetic provider satisfying the composition's adapter surface offline."""

    capabilities: frozenset[str] = frozenset()

    def __init__(self, *, unavailable: frozenset[str] = DEFAULT_UNAVAILABLE) -> None:
        """Configure the universe and the set of instruments left without history."""
        self._unavailable = unavailable
        self._gate = asyncio.Event()

    async def connect(self) -> None:
        """Open provider connections — none are needed offline."""

    async def disconnect(self) -> None:
        """Release provider connections and unblock the live stream."""
        self._gate.set()

    async def get_health(self) -> ProviderHealth:
        """Report a healthy provider observation."""
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=REFERENCE_INSTANT)

    async def load_instruments(self) -> tuple[Instrument, ...]:
        """Return the full synthetic instrument master."""
        return tuple(_instrument(symbol) for symbol in SYMBOLS)

    def load_nse_cash_equity_live_universe(self) -> DhanCashEquityLiveUniverse:
        """Resolve the synthetic cash-equity universe in canonical order."""
        return DhanCashEquityLiveUniverse(
            underlyings=(),
            cash_references=tuple(
                DhanInstrumentReference(
                    instrument=_instrument(symbol),
                    security_id=f"SEC-{symbol}",
                    underlying_security_id=None,
                    exchange_segment="NSE_EQ",
                    provider_instrument_type="ES",
                )
                for symbol in SYMBOLS
            ),
            missing_underlyings=(),
            ambiguous_underlyings=(),
            symbol_mismatches=(),
        )

    async def load_historical_data(self, request: HistoricalRequest) -> HistoricalResult:
        """Return one authoritative previous-session candle, or no history when unavailable."""
        symbol = request.instrument.symbol
        if symbol in self._unavailable:
            return HistoricalResult(request=request, candles=())
        close = _close_for(symbol)
        candle = Candle(
            instrument=request.instrument,
            start_timestamp=request.start_timestamp,
            end_timestamp=request.start_timestamp + request.interval,
            open_price=Decimal("100"),
            high_price=Decimal("140"),
            low_price=Decimal("160") - close,
            close_price=close,
            traded_quantity=1000,
        )
        return HistoricalResult(request=request, candles=(candle,))

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        """Yield one live tick per requested instrument (canonical order), then block."""
        for instrument in request.instruments:
            yield Tick(
                instrument=instrument,
                event_timestamp=REFERENCE_INSTANT,
                last_price=Decimal("101.25"),
                traded_quantity=10,
            )
        await self._gate.wait()
