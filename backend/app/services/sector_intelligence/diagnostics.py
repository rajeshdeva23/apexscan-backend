"""Bounded diagnostic counters for the passive sector shadow runtime (SECTOR-VIEW-1B).

Counters only — never an unbounded event history or per-tick log. The mutable
:class:`ShadowDiagnostics` is updated on the (synchronous) hot path and at evaluation; a frozen
:class:`ShadowDiagnosticsView` is copied out for safe external reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.market_intelligence.sector.models import FrozenModel


class ShadowDiagnosticsView(FrozenModel):
    """An immutable, copy-safe snapshot of the runtime's bounded counters."""

    events_received: int
    events_accepted: int
    events_rejected: int
    unknown_instruments: int
    duplicate_events: int
    out_of_order_events: int
    late_trading_date_events: int
    rollovers: int
    snapshot_attempts: int
    snapshot_successes: int
    snapshot_failures: int
    evaluation_overruns: int
    last_evaluation_duration_ms: float | None
    last_success_timestamp: str | None


@dataclass(slots=True)
class ShadowDiagnostics:
    """Mutable, bounded counters. All O(1) integer/float fields — never a growing collection."""

    events_received: int = 0
    events_accepted: int = 0
    events_rejected: int = 0
    unknown_instruments: int = 0
    duplicate_events: int = 0
    out_of_order_events: int = 0
    late_trading_date_events: int = 0
    rollovers: int = 0
    snapshot_attempts: int = 0
    snapshot_successes: int = 0
    snapshot_failures: int = 0
    evaluation_overruns: int = 0
    last_evaluation_duration_ms: float | None = None
    last_success_timestamp: str | None = field(default=None)

    def view(self) -> ShadowDiagnosticsView:
        """Return an immutable copy of the current counters for safe external reads."""
        return ShadowDiagnosticsView(
            events_received=self.events_received,
            events_accepted=self.events_accepted,
            events_rejected=self.events_rejected,
            unknown_instruments=self.unknown_instruments,
            duplicate_events=self.duplicate_events,
            out_of_order_events=self.out_of_order_events,
            late_trading_date_events=self.late_trading_date_events,
            rollovers=self.rollovers,
            snapshot_attempts=self.snapshot_attempts,
            snapshot_successes=self.snapshot_successes,
            snapshot_failures=self.snapshot_failures,
            evaluation_overruns=self.evaluation_overruns,
            last_evaluation_duration_ms=self.last_evaluation_duration_ms,
            last_success_timestamp=self.last_success_timestamp,
        )
