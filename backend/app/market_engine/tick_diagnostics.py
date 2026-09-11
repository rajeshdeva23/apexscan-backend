"""Bounded, read-only TickEngine acceptance diagnostics (FIX-1).

Aggregate counters that observe the engine's *existing* accept/reject decision without
changing it: every ``TickEngine.process`` outcome is counted by its actual
:class:`ValidationOutcome`, and a few bounded scalars record the last decision. These exist
to root-cause why live ticks are (or are not) accepted — in particular by exposing
``event_timestamp - now`` at the rejection decision, so a future-timestamp cause is
observable without decoding raw packets. Cardinality is O(1): no per-instrument, per-symbol,
or per-tick history is retained.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.market_engine.validation import ValidationOutcome

# The reject reasons a Tick/Quote can carry (ACCEPT is not a rejection). Fixed, bounded set.
_REJECT_REASONS = (
    ValidationOutcome.INVALID,
    ValidationOutcome.DUPLICATE,
    ValidationOutcome.STALE,
)


@dataclass(frozen=True, slots=True)
class TickEngineDiagnostics:
    """An immutable snapshot of TickEngine acceptance outcomes (bounded cardinality).

    ``rejected_by_reason`` is keyed by the actual :class:`ValidationOutcome` value of each
    rejection (``invalid`` / ``duplicate`` / ``stale``); it never contains ``accept``.
    ``last_rejected_event_clock_delta_seconds`` is ``event_timestamp - now`` (seconds) as
    measured at the last rejection using the *same* clock instant the engine validated with —
    a positive value near ``19800`` (~+05:30) is the signature of an exchange/UTC timestamp
    mismatch. Reference (previous-close) events are counted separately from ticks so the
    tick invariant ``ticks_received == ticks_accepted + ticks_rejected_total`` holds exactly.
    """

    ticks_received: int = 0
    ticks_accepted: int = 0
    ticks_rejected_total: int = 0
    rejected_by_reason: dict[str, int] = field(default_factory=dict)
    references_received: int = 0
    references_accepted: int = 0
    references_rejected: int = 0
    last_accepted_event_timestamp: datetime | None = None
    last_rejected_reason: str | None = None
    last_rejected_event_timestamp: datetime | None = None
    last_rejection_observed_at: datetime | None = None
    last_rejected_event_clock_delta_seconds: float | None = None


class TickEngineDecisionCounters:
    """Mutable, bounded counters over TickEngine decisions (no per-instrument state).

    Updated on the engine hot path with O(1) scalar writes only — no I/O, no logging, no
    unbounded collections. The engine is single-consumer (one ingestion loop on one event
    loop; ``process`` is synchronous and never awaits), so plain attribute mutation is safe
    and no lock is taken.
    """

    def __init__(self) -> None:
        """Start all counters at zero with no recorded last-decision state."""
        self._ticks_received = 0
        self._ticks_accepted = 0
        self._by_reason: dict[str, int] = {reason.value: 0 for reason in _REJECT_REASONS}
        self._references_received = 0
        self._references_accepted = 0
        self._references_rejected = 0
        self._last_accepted_event_ts: datetime | None = None
        self._last_rejected_reason: str | None = None
        self._last_rejected_event_ts: datetime | None = None
        self._last_rejection_observed_at: datetime | None = None
        self._last_rejected_delta_seconds: float | None = None

    def record_tick(
        self, outcome: ValidationOutcome, *, event_timestamp: datetime, now: datetime
    ) -> None:
        """Count one Tick/Quote decision using the exact clock instant validation used.

        Args:
            outcome: The engine's classification (never recomputed here).
            event_timestamp: The event's own timestamp.
            now: The clock instant the engine passed to ``classify`` for this event.
        """
        self._ticks_received += 1
        if outcome is ValidationOutcome.ACCEPT:
            self._ticks_accepted += 1
            self._last_accepted_event_ts = event_timestamp
            return
        self._by_reason[outcome.value] = self._by_reason.get(outcome.value, 0) + 1
        self._last_rejected_reason = outcome.value
        self._last_rejected_event_ts = event_timestamp
        self._last_rejection_observed_at = now
        self._last_rejected_delta_seconds = (event_timestamp - now).total_seconds()

    def record_reference(self, outcome: ValidationOutcome) -> None:
        """Count one MarketReference decision (previous-close), separate from ticks."""
        self._references_received += 1
        if outcome is ValidationOutcome.ACCEPT:
            self._references_accepted += 1
        else:
            self._references_rejected += 1

    def snapshot(self) -> TickEngineDiagnostics:
        """Return an immutable copy of the current counters."""
        return TickEngineDiagnostics(
            ticks_received=self._ticks_received,
            ticks_accepted=self._ticks_accepted,
            ticks_rejected_total=self._ticks_received - self._ticks_accepted,
            rejected_by_reason=dict(self._by_reason),
            references_received=self._references_received,
            references_accepted=self._references_accepted,
            references_rejected=self._references_rejected,
            last_accepted_event_timestamp=self._last_accepted_event_ts,
            last_rejected_reason=self._last_rejected_reason,
            last_rejected_event_timestamp=self._last_rejected_event_ts,
            last_rejection_observed_at=self._last_rejection_observed_at,
            last_rejected_event_clock_delta_seconds=self._last_rejected_delta_seconds,
        )
