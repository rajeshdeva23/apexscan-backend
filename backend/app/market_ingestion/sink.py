"""Non-authoritative provider-only event sink for the ingestion service (DECOUPLING PHASE H2).

With IPC publication OFF (H2), decoded canonical events must go *somewhere explicit* — never
silently dropped without a declared mode. This sink is that explicit destination: it keeps only a
bounded, credential-free event count and does **nothing else**. It never mutates a
TickEngine/MarketContext, never touches Redis, never allocates an M1 epoch, and never publishes
IPC. H3 replaces it with a real IPC publication path (and adds any per-kind comparison metrics
shadow validation needs).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.market_data import MarketData


@dataclass(frozen=True, slots=True)
class ProviderSinkDiagnostics:
    """Bounded snapshot of what the provider-only sink has observed."""

    events_total: int


class ProviderOnlyEventSink:
    """Counts decoded canonical events; performs no publication or authority mutation."""

    def __init__(self) -> None:
        self._events = 0

    def handle(self, datum: MarketData) -> None:
        """Record one decoded event (bounded counting only — no I/O, no side effects)."""
        self._events += 1

    def diagnostics(self) -> ProviderSinkDiagnostics:
        """Snapshot the bounded event counter."""
        return ProviderSinkDiagnostics(events_total=self._events)
