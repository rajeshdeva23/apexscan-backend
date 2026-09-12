"""Non-authoritative provider-only event sink for the ingestion service (DECOUPLING PHASE H2).

With IPC publication OFF (H2), decoded canonical events must go *somewhere explicit* — never
silently dropped without a declared mode. This sink is that explicit destination: it keeps only
bounded, credential-free counters (no per-instrument cardinality) and does **nothing else**. It
never mutates a TickEngine/MarketContext, never touches Redis, never allocates an M1 epoch, and
never publishes IPC. H3 replaces it with a real IPC publication path.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.market_data import MarketData, MarketReference, Quote, Tick


@dataclass(frozen=True, slots=True)
class ProviderSinkDiagnostics:
    """Bounded snapshot of what the provider-only sink has observed (no cardinality growth)."""

    events_total: int
    tick_total: int
    quote_total: int
    reference_total: int
    other_total: int


class ProviderOnlyEventSink:
    """Counts decoded canonical events by kind; performs no publication or authority mutation."""

    def __init__(self) -> None:
        self._events = 0
        self._ticks = 0
        self._quotes = 0
        self._references = 0
        self._other = 0

    def handle(self, datum: MarketData) -> None:
        """Record one decoded event (bounded counting only — no I/O, no side effects)."""
        self._events += 1
        if isinstance(datum, Tick):
            self._ticks += 1
        elif isinstance(datum, Quote):
            self._quotes += 1
        elif isinstance(datum, MarketReference):
            self._references += 1
        else:
            self._other += 1

    def diagnostics(self) -> ProviderSinkDiagnostics:
        """Snapshot the bounded per-kind counters."""
        return ProviderSinkDiagnostics(
            events_total=self._events,
            tick_total=self._ticks,
            quote_total=self._quotes,
            reference_total=self._references,
            other_total=self._other,
        )
