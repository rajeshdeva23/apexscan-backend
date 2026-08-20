"""Narrow capability ports the Strategy Manager depends on (P5.4; ADR-007 D8).

The manager orchestrates requirement provisioning without importing the Market
Engine's mutation engines: it depends on *capabilities*, not implementations. A
:class:`LiveTimeframeSink` receives only the effective timeframe set (never a
strategy, consumer key, or provider concept); a :class:`HistoricalWarmupPort`
warms the effective historical requirements over the instrument universe and
reports which timeframes ended up satisfied per instrument. Concrete adapters that
bind these ports to the real ``CandleEngine`` / ``HistoricalWarmupService`` live in
the composition layer (``app.services``), outside the guarded manager package.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from app.market_engine.historical.requirements import HistoricalRequirement
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument


@runtime_checkable
class LiveTimeframeSink(Protocol):
    """The additive Market-Engine seam that receives the effective live timeframe set."""

    def set_required_timeframes(self, timeframes: frozenset[Timeframe]) -> None:
        """Apply the effective required timeframe set (strategy-blind, timeframes only)."""
        ...


@runtime_checkable
class HistoricalWarmupPort(Protocol):
    """Warms effective historical requirements and reports satisfied timeframes."""

    async def warmup(
        self,
        instruments: Sequence[Instrument],
        effective_requirements: Sequence[HistoricalRequirement],
        *,
        reference: datetime,
    ) -> Mapping[Instrument, frozenset[Timeframe]]:
        """Warm the requirements over the universe and return satisfied timeframes.

        Args:
            instruments: The instrument universe to warm.
            effective_requirements: The deduplicated union of historical requirements.
            reference: The deterministic reference instant (UTC, tz-aware).

        Returns:
            A mapping of each instrument to the set of historical timeframes that
            were fully satisfied for it.
        """
        ...


@runtime_checkable
class SessionStatisticsRefreshControl(Protocol):
    """Receives the effective session-statistics demand so infrastructure can (de)activate.

    Broker-neutral: it carries only the strictest required ``max_age`` (or ``None`` to
    deactivate when no consumer requires the fact) — never a strategy, provider, source,
    HTTP, or instrument-universe concept (ADR-009 D2/§47). The concrete refresh
    coordinator that drives fetching/staging lives in the composition layer.
    """

    def configure(self, *, max_age: timedelta | None) -> None:
        """Set the effective refresh demand; ``None`` deactivates session-statistics refresh."""
        ...
