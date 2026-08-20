"""Manager-owned registry of per-consumer session-statistics demand (P4.6E5; ADR-009 D6).

A small, generic registry mapping an opaque ``consumer_key`` (a ``strategy_id``) to its
declared SESSION_STATISTICS freshness bound. The Strategy Manager registers/releases
entries across the ADR-007 lifecycle; the effective demand activates the shared refresh
infrastructure with the strictest (minimum) required ``max_age``. It holds no strategy
internals, no provider identity, and no instrument universe — WHAT is needed lives here,
FOR WHICH instruments is a composition concern (ADR-009 §15/§48).
"""

from __future__ import annotations

from datetime import timedelta


class FactRequirementRegistry:
    """Tracks each consumer's SESSION_STATISTICS freshness bound and the effective union."""

    def __init__(self) -> None:
        """Create an empty registry with no session-statistics consumers."""
        self._by_consumer: dict[str, timedelta] = {}

    def register(self, consumer_key: str, *, session_statistics_max_age: timedelta | None) -> None:
        """Register (or replace) a consumer's SESSION_STATISTICS freshness bound.

        A ``None`` bound means the consumer does not require session statistics (or
        declared it without a freshness bound); the consumer is removed so it neither
        activates the fact nor constrains the cadence. Re-registering the same consumer
        replaces its entry (an ERROR→START restart never double-registers).

        Args:
            consumer_key: The opaque, registry-local consumer key (a ``strategy_id``).
            session_statistics_max_age: The consumer's strictly-positive max age, or
                ``None`` when it does not activate session-statistics demand.

        Raises:
            ValueError: If ``consumer_key`` is empty.
        """
        if not consumer_key.strip():
            raise ValueError("consumer key must be non-empty")
        if session_statistics_max_age is None:
            self._by_consumer.pop(consumer_key, None)
            return
        self._by_consumer[consumer_key] = session_statistics_max_age

    def deregister(self, consumer_key: str) -> None:
        """Remove a consumer's demand (no-op if absent)."""
        self._by_consumer.pop(consumer_key, None)

    def is_active(self) -> bool:
        """Return whether any consumer currently requires session statistics."""
        return bool(self._by_consumer)

    def effective_session_statistics_max_age(self) -> timedelta | None:
        """Return the strictest (minimum) required max age, or ``None`` when inactive."""
        if not self._by_consumer:
            return None
        return min(self._by_consumer.values())
