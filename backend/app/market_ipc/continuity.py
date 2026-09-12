"""Broker-neutral feed-continuity runtime for the decoupled publication path (DECOUPLING L1).

The existing :class:`app.schemas.market_data.FeedContinuity` is a *market-data-quality* fact
(provider connectivity) consumed by the in-process candle engine. This module is a DIFFERENT,
non-competing concern: the **canonical publication continuity** of the future decoupled producer
path. It distinguishes four dimensions that must never be collapsed into one another
(DESIGN-REVIEW-2 / L1 §4):

    A. provider connectivity          (socket up/down)
    B. canonical producer progression (M1 identity + producer_sequence)
    C. IPC publication acceptance     (M2 queue accepted the event — NOT durability)
    D. IPC publication completion     (D1 confirmed the Redis publish)

Continuity relies on **explicit** failure evidence (queue overflow, worker fault, publication
failure/uncertainty, provider disconnect, incomplete drain) — never on producer_sequence
arithmetic: a gap can be a legally rejected/overflowed sequence, not provider packet loss
(§6/§7). A terminal publication break is **sticky for one producer incarnation** and is cleared
only by a new incarnation (new producer_epoch, §19), never by a provider reconnect (§16).

This tracker is purely observational: every method is O(1), in-memory, and does no I/O
(§20). It claims neither exactly-once nor cross-process dedup, does not activate IPC/M2/C1, and
does not resolve C1's authoritative apply→mark window (Phase H owns that). It is off by default
— nothing composes it into the production/default runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.market_ipc.boundary import (
    BoundaryDiagnostics,
    BoundaryState,
    DrainResult,
    SubmitOutcome,
)
from app.market_ipc.publisher import PublishOutcome


class ContinuityState(StrEnum):
    """Publication-continuity lifecycle for one producer incarnation."""

    NOT_STARTED = "not_started"
    HEALTHY = "healthy"  # no continuity break observed for this incarnation
    PROVIDER_DEGRADED = "provider_degraded"  # recoverable: provider down / awaiting evidence
    BROKEN = "broken"  # TERMINAL for this incarnation (overflow / worker fault / publish failure)
    STOPPING = "stopping"
    STOPPED = "stopped"


class ContinuityReason(StrEnum):
    """Machine-readable cause accompanying the state (never a bare ambiguous boolean)."""

    NONE = "none"
    PROVIDER_DISCONNECTED = "provider_disconnected"
    AWAITING_RECOVERY_EVIDENCE = "awaiting_recovery_evidence"
    PUBLICATION_QUEUE_OVERFLOW = "publication_queue_overflow"
    PUBLICATION_WORKER_FAILED = "publication_worker_failed"
    PUBLICATION_FAILED = "publication_failed"
    PUBLICATION_OUTCOME_UNCERTAIN = "publication_outcome_uncertain"
    INCOMPLETE_DRAIN = "incomplete_drain"
    CLEAN_SHUTDOWN = "clean_shutdown"


# Terminal break reasons: once set they persist for the incarnation until a new epoch resets it.
_TERMINAL_REASONS = frozenset(
    {
        ContinuityReason.PUBLICATION_QUEUE_OVERFLOW,
        ContinuityReason.PUBLICATION_WORKER_FAILED,
        ContinuityReason.PUBLICATION_FAILED,
        ContinuityReason.PUBLICATION_OUTCOME_UNCERTAIN,
    }
)


@dataclass(frozen=True, slots=True)
class FeedContinuitySnapshot:
    """Bounded, credential-free continuity snapshot (diagnostics / future Phase-H gating)."""

    state: ContinuityState
    reason: ContinuityReason
    producer_id: str | None
    producer_epoch: int | None
    provider_connected: bool
    worker_running: bool
    last_accepted_sequence: int | None
    last_published_sequence: int | None
    queue_depth: int
    queue_capacity: int
    pending_at_stop: int
    incomplete_drain: bool
    # Cumulative bounded counters (retained across incarnations for observability).
    continuity_break_total: int
    provider_disconnect_total: int
    provider_reconnect_total: int
    publication_failure_total: int
    publication_uncertain_total: int
    overflow_total: int
    prepare_rejected_total: int


class FeedContinuityTracker:
    """Observes normalized runtime signals and reports truthful publication continuity (L1).

    Wire it at the decoupled producer seams: :meth:`record_submission` at the M2 submit boundary
    and :meth:`record_publication` at the D1 transmit boundary track accepted/published positions.
    :meth:`observe_boundary` (from the boundary's public diagnostics) is **mandatory** for
    worker-fault detection: once the M2 worker faults the boundary goes ``FAILED`` — ``submit``
    then returns ``REJECTED_NOT_RUNNING`` and ``transmit`` stops being called, so the submit/
    publication seams alone can never observe the fault. A Phase-H composition MUST drive
    :meth:`observe_boundary` (or equivalent diagnostics polling), and MUST supply a **fresh**
    boundary per producer incarnation (a new epoch resets this tracker's seen-counter baselines,
    which is correct only against a boundary whose cumulative counters also restart at zero — the
    documented M2 lifecycle: new epoch = new ``publisher.start()`` = new boundary). It never
    infers loss from sequence gaps and never clears a terminal break on a provider reconnect.
    """

    def __init__(self) -> None:
        self._state = ContinuityState.NOT_STARTED
        self._reason = ContinuityReason.NONE
        self._producer_id: str | None = None
        self._producer_epoch: int | None = None
        self._provider_connected = False
        self._worker_running = False
        self._last_accepted: int | None = None
        self._last_published: int | None = None
        self._queue_depth = 0
        self._queue_capacity = 0
        self._pending_at_stop = 0
        self._incomplete_drain = False
        self._continuity_breaks = 0
        self._provider_disconnects = 0
        self._provider_reconnects = 0
        self._publication_failures = 0
        self._publication_uncertain = 0
        self._overflows = 0
        self._prepare_rejected = 0
        # Cumulative boundary totals last seen, so observe_boundary derives deltas idempotently.
        self._seen_overflow_total = 0
        self._seen_publish_failure_total = 0
        self._seen_published_total = 0

    # ----------------------------------------------------------------------- #
    # Producer incarnation (M1 identity)
    # ----------------------------------------------------------------------- #
    def producer_started(self, *, producer_id: str, producer_epoch: int) -> None:
        """Begin a producer incarnation; a NEW epoch resets a prior terminal break (§19).

        A repeat call for the same (producer_id, producer_epoch) is idempotent — an ordinary
        provider reconnect within the same process keeps the same epoch and must not reset state.
        """
        if self._producer_id == producer_id and self._producer_epoch == producer_epoch:
            return
        self._producer_id = producer_id
        self._producer_epoch = producer_epoch
        self._state = ContinuityState.HEALTHY
        self._reason = ContinuityReason.NONE
        self._provider_connected = False
        self._last_accepted = None
        self._last_published = None
        self._incomplete_drain = False
        self._pending_at_stop = 0
        self._seen_overflow_total = 0
        self._seen_publish_failure_total = 0
        self._seen_published_total = 0

    # ----------------------------------------------------------------------- #
    # Provider connectivity (dimension A)
    # ----------------------------------------------------------------------- #
    def provider_connected(self) -> None:
        """Observe provider socket up; never clears a terminal break, never healthy on its own."""
        was_connected = self._provider_connected
        self._provider_connected = True
        if not was_connected and self._state is not ContinuityState.NOT_STARTED:
            self._provider_reconnects += 1
        if self._state is ContinuityState.PROVIDER_DEGRADED:
            # Reconnect alone is not recovery: require a successful publication as evidence (§16).
            self._reason = ContinuityReason.AWAITING_RECOVERY_EVIDENCE

    def provider_disconnected(self) -> None:
        """Observe provider socket down (recoverable); a terminal break stays terminal."""
        self._provider_connected = False
        self._provider_disconnects += 1
        if self._state is ContinuityState.HEALTHY:
            self._state = ContinuityState.PROVIDER_DEGRADED
            self._reason = ContinuityReason.PROVIDER_DISCONNECTED

    # ----------------------------------------------------------------------- #
    # Publication acceptance (C) and completion (D)
    # ----------------------------------------------------------------------- #
    def publication_accepted(self, *, producer_sequence: int | None = None) -> None:
        """Record an event ACCEPTED into the M2 queue (NOT Redis durability; §10)."""
        if producer_sequence is not None:
            self._last_accepted = producer_sequence

    def publication_succeeded(self, *, producer_sequence: int | None = None) -> None:
        """Record a D1-confirmed publish; supplies recovery evidence for a degraded provider."""
        if producer_sequence is not None:
            self._last_published = producer_sequence
        if self._state is ContinuityState.PROVIDER_DEGRADED and self._provider_connected:
            self._state = ContinuityState.HEALTHY
            self._reason = ContinuityReason.NONE

    def publication_failed(self, *, producer_sequence: int | None = None) -> None:
        """Record a DEFINITE publication failure — terminal break (§13). Sequence not advanced."""
        self._publication_failures += 1
        self._break(ContinuityReason.PUBLICATION_FAILED)

    def publication_uncertain(self, *, producer_sequence: int | None = None) -> None:
        """Record an UNKNOWN publication outcome — terminal, never claims loss or success (§14)."""
        self._publication_uncertain += 1
        self._break(ContinuityReason.PUBLICATION_OUTCOME_UNCERTAIN)

    def publication_overflow(self) -> None:
        """Record an M2 REJECTED_OVERFLOW — a canonical event was not accepted; terminal (§11)."""
        self._overflows += 1
        self._break(ContinuityReason.PUBLICATION_QUEUE_OVERFLOW)

    def prepare_rejected(self) -> None:
        """Record an invalid-event prepare rejection (unsupported/oversize/serialization).

        A data-validity rejection, NOT a transport continuity break, so it is counted but not
        terminal (the pipeline is healthy; the event itself was malformed).
        """
        self._prepare_rejected += 1

    def worker_failed(self) -> None:
        """Record a terminal M2 worker fault — terminal break, no silent self-recovery (§12)."""
        self._break(ContinuityReason.PUBLICATION_WORKER_FAILED)

    # ----------------------------------------------------------------------- #
    # Shutdown (§17/§18)
    # ----------------------------------------------------------------------- #
    def begin_shutdown(self) -> None:
        """Mark the incarnation stopping (accepts no meaning about drain completeness yet)."""
        if self._state in (ContinuityState.NOT_STARTED, ContinuityState.STOPPED):
            return
        self._state = ContinuityState.STOPPING

    def drain_completed(self, result: DrainResult) -> None:
        """Finalize shutdown: clean only with a complete drain AND no unresolved terminal break."""
        was_broken = self._reason in _TERMINAL_REASONS
        self._pending_at_stop = result.pending_at_stop
        self._state = ContinuityState.STOPPED
        if not result.drained_complete or result.pending_at_stop > 0:
            self._incomplete_drain = True
            self._reason = ContinuityReason.INCOMPLETE_DRAIN
        elif not was_broken:
            self._reason = ContinuityReason.CLEAN_SHUTDOWN
        # else: keep the terminal break reason — a clean drain never hides a prior break.

    # ----------------------------------------------------------------------- #
    # Non-invasive M2 bridge (drives continuity from the boundary's public diagnostics only)
    # ----------------------------------------------------------------------- #
    def record_submission(
        self, outcome: SubmitOutcome, *, producer_sequence: int | None = None
    ) -> None:
        """Map an M2 :class:`SubmitOutcome` to a continuity observation (submit-seam wiring)."""
        if outcome is SubmitOutcome.ENQUEUED:
            self.publication_accepted(producer_sequence=producer_sequence)
        elif outcome is SubmitOutcome.REJECTED_OVERFLOW:
            self.publication_overflow()
        elif outcome in _PREPARE_REJECTIONS:
            self.prepare_rejected()
        # REJECTED_NOT_RUNNING is a lifecycle state, not a continuity break; ignored.

    def record_publication(
        self, outcome: PublishOutcome, *, producer_sequence: int | None = None
    ) -> None:
        """Map a D1/publisher :class:`PublishOutcome` to a continuity observation (transmit-seam).

        ``PUBLISHED`` advances the published position; **every** non-``PUBLISHED`` outcome maps to
        a terminal :meth:`publication_failed`. In practice ``transmit`` returns only ``PUBLISHED``
        or ``FAILED_TRANSPORT``, and ``FAILED_TRANSPORT`` conflates a definite failure with an
        unknown outcome (the current D1/publisher contract cannot distinguish them) — so mapping
        it conservatively to a terminal break fabricates certainty in neither direction.
        """
        if outcome is PublishOutcome.PUBLISHED:
            self.publication_succeeded(producer_sequence=producer_sequence)
        else:
            self.publication_failed(producer_sequence=producer_sequence)

    def observe_boundary(self, diagnostics: BoundaryDiagnostics) -> None:
        """Derive continuity from the boundary's public diagnostics without touching M2 internals.

        Updates the live worker/queue view and turns cumulative-counter increases into terminal
        breaks (worker fault, overflow, publish failure) and recovery evidence (new publishes).
        Delta-based so repeated calls are idempotent.
        """
        self._worker_running = diagnostics.worker_running
        self._queue_depth = diagnostics.queue_depth
        self._queue_capacity = diagnostics.queue_capacity
        if diagnostics.state is BoundaryState.FAILED:
            self.worker_failed()
        for _ in range(diagnostics.overflow_total - self._seen_overflow_total):
            self.publication_overflow()
        self._seen_overflow_total = diagnostics.overflow_total
        for _ in range(diagnostics.publish_failure_total - self._seen_publish_failure_total):
            self.publication_failed()
        self._seen_publish_failure_total = diagnostics.publish_failure_total
        if diagnostics.published_total > self._seen_published_total:
            self.publication_succeeded()
        self._seen_published_total = diagnostics.published_total

    # ----------------------------------------------------------------------- #
    # Internal + snapshot
    # ----------------------------------------------------------------------- #
    def _break(self, reason: ContinuityReason) -> None:
        """Enter the terminal BROKEN state, keeping the first (root-cause) break reason."""
        if self._state is not ContinuityState.BROKEN:
            self._state = ContinuityState.BROKEN
            self._reason = reason
            self._continuity_breaks += 1

    @property
    def state(self) -> ContinuityState:
        """Current publication-continuity state."""
        return self._state

    def snapshot(self) -> FeedContinuitySnapshot:
        """Return the bounded, credential-free continuity snapshot."""
        return FeedContinuitySnapshot(
            state=self._state,
            reason=self._reason,
            producer_id=self._producer_id,
            producer_epoch=self._producer_epoch,
            provider_connected=self._provider_connected,
            worker_running=self._worker_running,
            last_accepted_sequence=self._last_accepted,
            last_published_sequence=self._last_published,
            queue_depth=self._queue_depth,
            queue_capacity=self._queue_capacity,
            pending_at_stop=self._pending_at_stop,
            incomplete_drain=self._incomplete_drain,
            continuity_break_total=self._continuity_breaks,
            provider_disconnect_total=self._provider_disconnects,
            provider_reconnect_total=self._provider_reconnects,
            publication_failure_total=self._publication_failures,
            publication_uncertain_total=self._publication_uncertain,
            overflow_total=self._overflows,
            prepare_rejected_total=self._prepare_rejected,
        )


_PREPARE_REJECTIONS = frozenset(
    {
        SubmitOutcome.FAILED_UNSUPPORTED,
        SubmitOutcome.FAILED_OVERSIZE,
        SubmitOutcome.FAILED_SERIALIZATION,
    }
)
