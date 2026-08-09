"""Market Engine domain events published on the shared in-process bus.

The engine publishes versioned "MarketContext" events and never calls consumers
directly (docs/06 §19, docs/01 §9.2 Event 2). Only the two context-lifecycle
events needed by the foundation are defined here; strategy-evaluation events
(Event 3) and their consumers belong to Phase 5. Events are immutable so a
published payload can never be mutated by a subscriber.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.events.bus import Event
from app.market_engine.context import MarketContext


@dataclass(frozen=True, slots=True)
class MarketContextCreated(Event):
    """Signals that the first MarketContext for an instrument has been built.

    Attributes:
        context: The immutable version-1 snapshot.
    """

    context: MarketContext


@dataclass(frozen=True, slots=True)
class MarketContextUpdated(Event):
    """Signals that a new MarketContext version is available for an instrument.

    Attributes:
        context: The immutable new snapshot.
        previous_version: The version this snapshot supersedes.
    """

    context: MarketContext
    previous_version: int
