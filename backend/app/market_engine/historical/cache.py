"""Exact-coverage in-memory cache of authoritative historical candles (P4.5B).

One bounded entry per (instrument, timeframe) holds the authoritative candles for
a contiguous fetched window and the window's coverage. A lookup is a hit only when
the requested window is *fully* contained in the entry's coverage — partial
coverage is never treated as complete (docs/06 §14.2; ADR-006 complete-or-withhold).
A larger fetched window therefore serves later, smaller lookbacks. Nothing is
persisted (no PostgreSQL, no Redis — docs/02 §7); retention is bounded by the
active timeframes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.market_engine.historical.source import HistoricalRequestKey
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, Instrument


@dataclass(frozen=True, slots=True)
class _Entry:
    """One instrument/timeframe cache entry: candles plus their coverage window."""

    candles: tuple[Candle, ...]
    coverage_start: datetime
    coverage_end: datetime

    def covers(self, start: datetime, end: datetime) -> bool:
        """Return whether ``[start, end)`` is fully within this entry's coverage."""
        return self.coverage_start <= start and end <= self.coverage_end


class HistoricalCache:
    """A bounded, in-memory, exact-coverage cache keyed by (instrument, timeframe)."""

    def __init__(self) -> None:
        """Create an empty cache."""
        self._entries: dict[tuple[Instrument, Timeframe], _Entry] = {}

    def get(self, key: HistoricalRequestKey) -> tuple[Candle, ...] | None:
        """Return the authoritative candles for a fully-covered request, else ``None``.

        On a hit, returns exactly the candles whose start falls in the requested
        ``[start, end)`` window (an immutable tuple). A request only partially
        covered by the entry is a miss.

        Args:
            key: The broker-neutral request identity.

        Returns:
            The covered candles, or ``None`` when coverage is insufficient.
        """
        entry = self._entries.get((key.instrument, key.timeframe))
        if entry is None or not entry.covers(key.start, key.end):
            return None
        return tuple(
            candle for candle in entry.candles if key.start <= candle.start_timestamp < key.end
        )

    def put(self, key: HistoricalRequestKey, candles: tuple[Candle, ...]) -> None:
        """Store authoritative candles for a fetched window, replacing any prior entry.

        Args:
            key: The broker-neutral request identity whose window was fetched.
            candles: The authoritative candles returned for that window.
        """
        self._entries[(key.instrument, key.timeframe)] = _Entry(
            candles=candles, coverage_start=key.start, coverage_end=key.end
        )

    def retain_timeframes(self, active: frozenset[Timeframe]) -> None:
        """Evict entries whose timeframe is no longer among the active requirements.

        Args:
            active: The timeframes still required; entries for others are dropped.
        """
        stale = [entry_key for entry_key in self._entries if entry_key[1] not in active]
        for entry_key in stale:
            del self._entries[entry_key]

    def entry_count(self) -> int:
        """Return the number of cached (instrument, timeframe) entries."""
        return len(self._entries)
