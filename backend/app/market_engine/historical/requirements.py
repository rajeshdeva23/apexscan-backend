"""Broker-neutral historical requirements and their deterministic union (P4.5A).

A :class:`HistoricalRequirement` declares *what* shape of history a consumer
needs — a timeframe and a candle lookback count — and nothing about *how* it is
fetched (no instrument, provider, strategy, date range, or transport concept).
The :class:`HistoricalRequirementRegistry` collects requirements from opaque
consumer keys and folds them into one deterministic, deduplicated effective set;
the consumer key is registry-local and never leaks into a requirement, a series,
a context, or a provider request (ADR-003, ADR-006 §7; docs/06 §14.2).

This module is pure and deterministic: no I/O, no provider capability checks, no
timeframe allowlist — a 7m requirement is as valid here as a 5m one, whether or
not any provider can yet fulfil it (that belongs to later P4.5 slices).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.market_engine.timeframe import Timeframe

_MIN_LOOKBACK = 1


def timeframe_ordering_key(timeframe: Timeframe) -> tuple[bool, float]:
    """Return a deterministic total-order key for a timeframe.

    Intraday timeframes sort first, by ascending duration; the whole-session
    timeframe sorts last. The key depends only on the timeframe's own data, so
    the same set of timeframes always yields the same order regardless of how or
    when they were registered.

    Args:
        timeframe: The timeframe to derive an ordering key for.

    Returns:
        A ``(is_session, duration_seconds)`` tuple usable as a ``sorted`` key.
    """
    duration = timeframe.duration
    seconds = duration.total_seconds() if duration is not None else 0.0
    return (timeframe.is_session, seconds)


@dataclass(frozen=True, slots=True)
class HistoricalRequirement:
    """An immutable, hashable declaration of a required historical data shape.

    Attributes:
        timeframe: The timeframe the history is required at.
        lookback: The number of candles required (must be at least one).
    """

    timeframe: Timeframe
    lookback: int

    def __post_init__(self) -> None:
        """Reject a non-positive lookback, failing fast at construction."""
        if self.lookback < _MIN_LOOKBACK:
            raise ValueError("historical lookback must be a positive integer")


class HistoricalRequirementRegistry:
    """Collects per-consumer requirements and folds them into a deterministic union.

    Consumers register under an opaque, registry-local key. Registering again for
    the same key replaces that consumer's requirements. The effective union takes,
    per timeframe, the maximum lookback across all consumers, so overlapping needs
    share one result rather than multiplying (docs/06 §14.2). The consumer key is
    never exposed in the effective requirements.
    """

    def __init__(self) -> None:
        """Create an empty registry with no registered consumers."""
        self._by_consumer: dict[str, frozenset[HistoricalRequirement]] = {}

    def register(self, consumer_key: str, requirements: Iterable[HistoricalRequirement]) -> None:
        """Register (or replace) the requirements for one consumer.

        Args:
            consumer_key: An opaque, registry-local identifier for the consumer.
                It is never interpreted and never leaves the registry.
            requirements: The requirements this consumer needs; duplicates within
                the set collapse.

        Raises:
            ValueError: If ``consumer_key`` is empty.
        """
        if not consumer_key.strip():
            raise ValueError("consumer key must be a non-empty string")
        self._by_consumer[consumer_key] = frozenset(requirements)

    def deregister(self, consumer_key: str) -> None:
        """Remove a consumer's requirements; a no-op if the key is not registered."""
        self._by_consumer.pop(consumer_key, None)

    def effective_requirements(self) -> tuple[HistoricalRequirement, ...]:
        """Return the deduplicated union of all registered requirements.

        Per timeframe, the maximum lookback across every consumer wins. The result
        is ordered deterministically by :func:`timeframe_ordering_key`, independent
        of registration order.

        Returns:
            An immutable, deterministically ordered tuple of requirements.
        """
        max_lookback: dict[Timeframe, int] = {}
        for requirements in self._by_consumer.values():
            for requirement in requirements:
                current = max_lookback.get(requirement.timeframe)
                if current is None or requirement.lookback > current:
                    max_lookback[requirement.timeframe] = requirement.lookback
        ordered = sorted(max_lookback.items(), key=lambda item: timeframe_ordering_key(item[0]))
        return tuple(
            HistoricalRequirement(timeframe=timeframe, lookback=lookback)
            for timeframe, lookback in ordered
        )
