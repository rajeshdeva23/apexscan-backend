"""Shadow-publish publication stack for the market-ingestion service (DECOUPLING PHASE H3A).

Wires the frozen H3 pipeline (ADR-026): decoded canonical event → :class:`PublishingEventSink` →
M2 :class:`AsyncPublicationBoundary` (non-blocking submit) → the ordered worker → D1
:class:`MarketEventPublisher`/:class:`RedisAtomicPublisher` → Redis stream/reference, with L1
:class:`FeedContinuityTracker` observing accepted/published positions and terminal breaks.

The sink calls ``M2.submit`` only (O(1), no Redis I/O, no D1/TickEngine call) and raises
:class:`PublicationTerminalError` on a terminal submit outcome (overflow, or the worker already
faulted) so provider intake fails closed instead of streaming into a dead publisher. Building the
stack constructs nothing live at import time; it is called by the composition root / tests with a
real or test Redis. IPC publication is producer-side only — there is no consumer, no C1, and the
backend TickEngine is never touched.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from redis.asyncio import Redis

from app.market_ingestion.errors import PublicationTerminalError
from app.market_ipc.atomic import RedisAtomicPublisher
from app.market_ipc.boundary import AsyncPublicationBoundary, SubmitOutcome
from app.market_ipc.config import MarketIpcConfig
from app.market_ipc.continuity import FeedContinuityTracker
from app.market_ipc.epoch import DurableEpochAllocator
from app.market_ipc.publisher import MarketEventPublisher, StaticUniverseVersion
from app.market_ipc.transport import RedisMarketEventStream
from app.schemas.market_data import MarketData

__all__ = [
    "PublicationStack",
    "PublicationTerminalError",
    "PublishingEventSink",
    "build_publication_stack",
]

# Submit outcomes that are terminal for the producer incarnation (fail closed → stop intake):
# an overflow dropped a canonical event, or the boundary is no longer RUNNING (worker faulted).
_TERMINAL_SUBMIT_OUTCOMES = frozenset(
    {SubmitOutcome.REJECTED_OVERFLOW, SubmitOutcome.REJECTED_NOT_RUNNING}
)


class _StaticTradingDate:
    """Provisional trading-date source for H3A transport composition (a fixed configured date)."""

    def __init__(self, trading_date: date) -> None:
        self._trading_date = trading_date

    def current_trading_date(self) -> date:
        """Return the configured provisional trading date."""
        return self._trading_date


class PublishingEventSink:
    """Routes each decoded canonical event to M2; fails closed on a terminal submit outcome."""

    def __init__(
        self,
        *,
        boundary: AsyncPublicationBoundary,
        continuity: FeedContinuityTracker,
        publisher: MarketEventPublisher,
    ) -> None:
        self._boundary = boundary
        self._continuity = continuity
        self._publisher = publisher

    def handle(self, datum: MarketData) -> None:
        """Submit one event to M2 and record it in L1; raise on a terminal break (no Redis I/O).

        On acceptance the just-allocated producer_sequence (an O(1) read, no diagnostics build) is
        recorded as the L1 accepted position — distinct from the published position, which the
        bounded observer advances from confirmed M2/D1 completions.
        """
        outcome = self._boundary.submit(datum)
        if outcome is SubmitOutcome.ENQUEUED:
            self._continuity.publication_accepted(
                producer_sequence=self._publisher.current_sequence
            )
            return
        if outcome is SubmitOutcome.REJECTED_OVERFLOW:
            self._continuity.publication_overflow()
        elif outcome is not SubmitOutcome.REJECTED_NOT_RUNNING:
            self._continuity.prepare_rejected()  # unsupported/oversize/serialization — non-terminal
        if outcome in _TERMINAL_SUBMIT_OUTCOMES:
            raise PublicationTerminalError(
                f"publication terminal break: submit outcome {outcome.value}"
            )


@dataclass(frozen=True, slots=True)
class PublicationStack:
    """The constructed M1/D1/M2/L1 publication components for one producer incarnation."""

    producer_id: str
    publisher: MarketEventPublisher
    boundary: AsyncPublicationBoundary
    continuity: FeedContinuityTracker
    sink: PublishingEventSink


def build_publication_stack(
    *,
    redis: Redis,
    config: MarketIpcConfig,
    producer_id: str,
    state_dir: Path,
    now: Callable[[], datetime],
    trading_date: date,
    universe_version: int = 0,
) -> PublicationStack:
    """Construct the H3 publication stack over ``redis`` (real or test); no epoch allocated yet.

    The M1 epoch is allocated later, by ``boundary.start()`` → ``publisher.start()`` (frozen
    startup order); constructing the stack performs no Redis I/O and no epoch allocation.
    """
    stream = RedisMarketEventStream(redis=redis, config=config)
    atomic_publisher = RedisAtomicPublisher(redis, config)
    publisher = MarketEventPublisher(
        stream=stream,
        config=config,
        producer_id=producer_id,
        epoch_allocator=DurableEpochAllocator(state_dir),
        trading_date_source=_StaticTradingDate(trading_date),
        universe_version_source=StaticUniverseVersion(universe_version),
        now=now,
        atomic_publisher=atomic_publisher,
    )
    boundary = AsyncPublicationBoundary(
        publisher=publisher,
        capacity=config.publish_queue_capacity,
        drain_timeout_seconds=config.publish_shutdown_drain_timeout_seconds,
        now=now,
    )
    continuity = FeedContinuityTracker()
    sink = PublishingEventSink(boundary=boundary, continuity=continuity, publisher=publisher)
    return PublicationStack(
        producer_id=producer_id,
        publisher=publisher,
        boundary=boundary,
        continuity=continuity,
        sink=sink,
    )
