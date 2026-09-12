"""Bounded asynchronous market-IPC publication boundary (DECOUPLING PHASE M2).

Phases B/D/D1 publish synchronously, so the ingestion callback waits on Redis latency/stalls.
M2 moves Redis I/O off the ingestion hot path: ``submit`` builds the immutable envelope
(fixing M1 identity) and enqueues it in O(1) with no network I/O; a single ordered worker
dequeues FIFO and calls the existing D1 publisher (``MarketEventPublisher.transmit``).

Invariants:
* the queue is bounded — a full queue fails submission **explicitly and immediately**
  (``REJECTED_OVERFLOW``), never silently dropping/coalescing/overwriting a canonical event and
  never blocking ingestion;
* strict FIFO — one worker, so Redis publication order == submission order == producer_sequence;
* identity is fixed at ``submit`` and carried through the queue unchanged (a worker retry never
  reallocates a sequence);
* fail-closed — before start / while stopping / after a terminal worker fault, ``submit`` is
  rejected; shutdown drains accepted items under a bounded timeout and surfaces an incomplete
  drain rather than silently discarding them.

M2 changes execution *placement* only: D1 atomicity and M1 identity are unchanged, IPC stays off
by default (nothing composes this), and it claims neither exactly-once nor cross-process dedup.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.market_ipc.envelope import MarketEventEnvelope
from app.market_ipc.publisher import MarketEventPublisher, PublishOutcome
from app.schemas.market_data import FeedContinuityEvent as _FeedContinuityEvent
from app.schemas.market_data import MarketData

PublishableEvent = MarketData | _FeedContinuityEvent


class BoundaryState(StrEnum):
    """Lifecycle state of the publication boundary."""

    NOT_STARTED = "not_started"
    RUNNING = "running"
    FAILED = "failed"  # terminal worker fault; submissions rejected
    STOPPING = "stopping"
    STOPPED = "stopped"


class SubmitOutcome(StrEnum):
    """Deterministic result of one non-blocking ``submit`` (never raises, never blocks)."""

    ENQUEUED = "enqueued"
    REJECTED_NOT_RUNNING = "rejected_not_running"  # before start / stopping / stopped / failed
    REJECTED_OVERFLOW = "rejected_overflow"  # bounded queue full — explicit, never silent
    FAILED_UNSUPPORTED = "failed_unsupported"  # prepare rejected the event (invalid kind)
    FAILED_OVERSIZE = "failed_oversize"  # prepare rejected the event (oversize payload)
    FAILED_SERIALIZATION = "failed_serialization"  # prepare failed to serialize the event


_PREPARE_OUTCOME_MAP = {
    PublishOutcome.FAILED_UNSUPPORTED: SubmitOutcome.FAILED_UNSUPPORTED,
    PublishOutcome.FAILED_OVERSIZE: SubmitOutcome.FAILED_OVERSIZE,
    PublishOutcome.FAILED_SERIALIZATION: SubmitOutcome.FAILED_SERIALIZATION,
}


@dataclass(frozen=True, slots=True)
class DrainResult:
    """Outcome of a graceful stop."""

    drained_complete: bool  # every accepted item published before the drain timeout
    pending_at_stop: int  # queue depth still unpublished when stop returned (0 when complete)


class BoundaryDiagnostics:
    """Bounded, credential-free publication-boundary status (no per-instrument cardinality)."""

    __slots__ = (
        "state",
        "queue_depth",
        "queue_capacity",
        "queue_high_watermark",
        "enqueued_total",
        "published_total",
        "publish_failure_total",
        "overflow_total",
        "prepare_rejected_total",
        "worker_running",
        "last_failure",
        "last_publish_success_at",
        "last_publish_failure_at",
    )

    def __init__(
        self,
        *,
        state: BoundaryState,
        queue_depth: int,
        queue_capacity: int,
        queue_high_watermark: int,
        enqueued_total: int,
        published_total: int,
        publish_failure_total: int,
        overflow_total: int,
        prepare_rejected_total: int,
        worker_running: bool,
        last_failure: str | None,
        last_publish_success_at: datetime | None,
        last_publish_failure_at: datetime | None,
    ) -> None:
        self.state = state
        self.queue_depth = queue_depth
        self.queue_capacity = queue_capacity
        self.queue_high_watermark = queue_high_watermark
        self.enqueued_total = enqueued_total
        self.published_total = published_total
        self.publish_failure_total = publish_failure_total
        self.overflow_total = overflow_total
        self.prepare_rejected_total = prepare_rejected_total
        self.worker_running = worker_running
        self.last_failure = last_failure
        self.last_publish_success_at = last_publish_success_at
        self.last_publish_failure_at = last_publish_failure_at


class AsyncPublicationBoundary:
    """Bounded, ordered, non-blocking async boundary in front of the D1 publisher (M2)."""

    def __init__(
        self,
        *,
        publisher: MarketEventPublisher,
        capacity: int,
        drain_timeout_seconds: float,
        now: Callable[[], datetime],
    ) -> None:
        """Wire the boundary to the D1 publisher with a bounded queue capacity and drain timeout."""
        if capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        if drain_timeout_seconds < 0:
            raise ValueError("drain_timeout_seconds must be non-negative")
        self._publisher = publisher
        self._capacity = capacity
        self._drain_timeout = drain_timeout_seconds
        self._now = now
        self._queue: asyncio.Queue[MarketEventEnvelope] = asyncio.Queue(maxsize=capacity)
        self._state = BoundaryState.NOT_STARTED
        self._worker: asyncio.Task[None] | None = None
        self._high_watermark = 0
        self._in_flight = 0  # items dequeued but not yet transmitted (for exact drain accounting)
        self._enqueued = 0
        self._published = 0
        self._publish_failures = 0
        self._overflow = 0
        self._prepare_rejected = 0
        self._last_failure: str | None = None
        self._last_success_at: datetime | None = None
        self._last_failure_at: datetime | None = None

    @property
    def state(self) -> BoundaryState:
        """Current lifecycle state."""
        return self._state

    async def start(self) -> None:
        """Start the D1 publisher (allocating the M1 epoch) and the ordered worker; fail closed.

        Raises whatever the publisher's ``start`` raises (e.g. Redis down / epoch unavailable) so
        a boundary that cannot establish identity never accepts submissions.
        """
        if self._state is not BoundaryState.NOT_STARTED:
            return
        await self._publisher.start()  # fail-closed: no worker/queue accepts before this succeeds
        self._worker = asyncio.create_task(self._run_worker())
        self._state = BoundaryState.RUNNING

    def submit(self, datum: PublishableEvent) -> SubmitOutcome:
        """Prepare + enqueue one event without blocking or any Redis I/O (never raises).

        Rejects (never blocks/drops silently) when not RUNNING or when the bounded queue is full.
        Prepare failures (unsupported/oversize/serialization) reject the event explicitly — the
        event was invalid, not silently lost.
        """
        if self._state is not BoundaryState.RUNNING:
            return SubmitOutcome.REJECTED_NOT_RUNNING
        prepared = self._publisher.prepare(datum)
        if isinstance(prepared, PublishOutcome):
            self._prepare_rejected += 1
            return _PREPARE_OUTCOME_MAP[prepared]
        try:
            self._queue.put_nowait(prepared)
        except asyncio.QueueFull:
            self._overflow += 1
            return SubmitOutcome.REJECTED_OVERFLOW
        self._enqueued += 1
        self._high_watermark = max(self._high_watermark, self._queue.qsize())
        return SubmitOutcome.ENQUEUED

    async def _run_worker(self) -> None:
        """Dequeue FIFO and transmit via the D1 publisher; one worker preserves order."""
        while True:
            envelope = await self._queue.get()
            self._in_flight += 1  # dequeued; counted as pending until transmit resolves
            try:
                outcome = await self._publisher.transmit(envelope)
            except asyncio.CancelledError:
                # Cancelled mid-transmit: the item is neither published nor re-queued. Keep it
                # counted as in-flight so stop() surfaces it in pending_at_stop (no silent loss);
                # task_done keeps join() accounting consistent.
                self._queue.task_done()
                raise
            except Exception as error:  # noqa: BLE001 - a terminal worker fault must be observable
                self._publish_failures += 1
                self._last_failure = type(error).__name__
                self._last_failure_at = self._now()
                self._state = BoundaryState.FAILED  # fail closed; reject further submissions
                self._in_flight -= 1
                self._queue.task_done()
                return
            if outcome is PublishOutcome.PUBLISHED:
                self._published += 1
                self._last_success_at = self._now()
            else:
                self._publish_failures += 1
                self._last_failure = outcome.value
                self._last_failure_at = self._now()
            self._in_flight -= 1
            self._queue.task_done()

    async def stop(self) -> DrainResult:
        """Reject new submissions, drain accepted items under the bounded timeout, stop the worker.

        Never waits forever and never silently discards accepted items: if the drain times out
        (e.g. Redis stalled or the worker failed), the still-pending depth is surfaced.
        """
        if self._state is BoundaryState.NOT_STARTED:
            return DrainResult(drained_complete=True, pending_at_stop=0)  # never ran; leave state
        if self._state is BoundaryState.STOPPED:
            return DrainResult(drained_complete=True, pending_at_stop=0)  # idempotent
        was_failed = self._state is BoundaryState.FAILED
        self._state = BoundaryState.STOPPING
        complete = True
        if not was_failed:  # a failed worker has exited; don't wait on a drain that can't progress
            try:
                await asyncio.wait_for(self._queue.join(), self._drain_timeout)
            except TimeoutError:
                complete = False
        # Count the item the worker holds mid-transmit (not in qsize) so an incomplete drain never
        # under-reports accepted-but-unpublished events.
        pending = self._queue.qsize() + self._in_flight
        await self._cancel_worker()
        self._state = BoundaryState.STOPPED
        return DrainResult(drained_complete=complete and pending == 0, pending_at_stop=pending)

    async def _cancel_worker(self) -> None:
        """Cancel and await the worker task, tolerating a normal cancellation."""
        if self._worker is None:
            return
        self._worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._worker
        self._worker = None

    def diagnostics(self) -> BoundaryDiagnostics:
        """Snapshot the bounded boundary counters/state."""
        worker_running = self._worker is not None and not self._worker.done()
        return BoundaryDiagnostics(
            state=self._state,
            queue_depth=self._queue.qsize(),
            queue_capacity=self._capacity,
            queue_high_watermark=self._high_watermark,
            enqueued_total=self._enqueued,
            published_total=self._published,
            publish_failure_total=self._publish_failures,
            overflow_total=self._overflow,
            prepare_rejected_total=self._prepare_rejected,
            worker_running=worker_running,
            last_failure=self._last_failure,
            last_publish_success_at=self._last_success_at,
            last_publish_failure_at=self._last_failure_at,
        )
