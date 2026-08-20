"""Manager-owned live-timeframe requirement registry and its union (P5.4; ADR-007 D8).

Symmetric to the Phase-4 :class:`HistoricalRequirementRegistry`: consumers register
a set of required live timeframes under an opaque, registry-local ``consumer_key``;
the effective set is the deterministic **union** across all consumers. A stopping
consumer never removes a timeframe another consumer still needs. Pure and
deterministic — no provider concepts, no strategy branches, no CandleEngine, no
MarketContext.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.market_engine.historical.requirements import timeframe_ordering_key
from app.market_engine.timeframe import Timeframe


class LiveTimeframeRequirementRegistry:
    """Collects per-consumer live timeframes and folds them into a deterministic union.

    Registering again for the same key **replaces** that consumer's timeframes
    (same semantics as :class:`HistoricalRequirementRegistry`). The consumer key is
    never exposed in the effective set.
    """

    def __init__(self) -> None:
        """Create an empty registry with no registered consumers."""
        self._by_consumer: dict[str, frozenset[Timeframe]] = {}

    def register(self, consumer_key: str, timeframes: Iterable[Timeframe]) -> None:
        """Register (or replace) the live timeframes for one consumer.

        Args:
            consumer_key: An opaque, registry-local identifier; never interpreted
                and never leaked into the effective set.
            timeframes: The timeframes this consumer needs; duplicates collapse.

        Raises:
            ValueError: If ``consumer_key`` is empty or whitespace.
        """
        if not consumer_key.strip():
            raise ValueError("consumer key must be a non-empty string")
        self._by_consumer[consumer_key] = frozenset(timeframes)

    def deregister(self, consumer_key: str) -> None:
        """Remove a consumer's timeframes; a no-op if the key is not registered."""
        self._by_consumer.pop(consumer_key, None)

    def requirements_for(self, consumer_key: str) -> frozenset[Timeframe]:
        """Return one consumer's registered timeframes (empty if not registered)."""
        return self._by_consumer.get(consumer_key, frozenset())

    def effective_timeframes(self) -> frozenset[Timeframe]:
        """Return the union of every consumer's required timeframes."""
        effective: set[Timeframe] = set()
        for timeframes in self._by_consumer.values():
            effective |= timeframes
        return frozenset(effective)

    def snapshot(self) -> tuple[tuple[str, tuple[Timeframe, ...]], ...]:
        """Return an immutable, deterministically ordered view of all consumers.

        Consumers are ordered by key; each consumer's timeframes are ordered by
        :func:`timeframe_ordering_key`. Independent of registration order.
        """
        return tuple(
            (consumer_key, tuple(sorted(timeframes, key=timeframe_ordering_key)))
            for consumer_key, timeframes in sorted(self._by_consumer.items())
        )
