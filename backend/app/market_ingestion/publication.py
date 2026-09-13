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
from typing import TYPE_CHECKING

from redis.asyncio import Redis

from app.market_ingestion.errors import PublicationTerminalError
from app.market_ingestion.sink import ProviderSinkDiagnostics
from app.market_ipc.atomic import RedisAtomicPublisher
from app.market_ipc.boundary import AsyncPublicationBoundary, SubmitOutcome
from app.market_ipc.config import MarketIpcConfig
from app.market_ipc.continuity import FeedContinuityTracker
from app.market_ipc.epoch import DurableEpochAllocator
from app.market_ipc.publisher import MarketEventPublisher, StaticUniverseVersion, TradingDateSource
from app.market_ipc.transport import RedisMarketEventStream
from app.schemas.market_data import MarketData

if TYPE_CHECKING:
    from app.market_engine.context import SessionContext

__all__ = [
    "PublicationStack",
    "PublicationTerminalError",
    "PublishingEventSink",
    "SessionTradingDate",
    "build_publication_stack",
]

# Submit outcomes that are terminal for the producer incarnation (fail closed → stop intake):
# an overflow dropped a canonical event, or the boundary is no longer RUNNING (worker faulted).
_TERMINAL_SUBMIT_OUTCOMES = frozenset(
    {SubmitOutcome.REJECTED_OVERFLOW, SubmitOutcome.REJECTED_NOT_RUNNING}
)


class SessionTradingDate:
    """Dynamic trading-date source: the exchange-local session date of the current instant.

    Replaces the H3A ``_StaticTradingDate`` so a long-lived producer crosses trading-date
    boundaries (an exchange-local midnight, a weekend, a holiday → the next session) without a
    restart. The publisher reads ``current_trading_date()`` once per event while preparing the
    envelope, so the reference key ``md:reference:<trading_date>`` follows the live session date
    automatically.

    The date comes from the canonical :class:`~app.market_engine.session.MarketSessionClassifier`
    (the market-IPC trading-date authority — see ``app.market_ipc.consumer``), which converts the
    instant to the exchange timezone *before* taking the date. A server/UTC wall-clock midnight
    therefore never shifts the trading date; only an exchange-local date change does.
    """

    def __init__(
        self,
        *,
        classify: Callable[[datetime], SessionContext],
        now: Callable[[], datetime],
    ) -> None:
        """Wire the source to a session classifier's ``classify`` and an injected UTC clock."""
        self._classify = classify
        self._now = now

    def current_trading_date(self) -> date:
        """Return the exchange-local trading date of the current instant (evaluated per event)."""
        return self._classify(self._now()).trading_date


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
        self._events = 0

    def diagnostics(self) -> ProviderSinkDiagnostics:
        """Total events routed to M2 (parity with the provider-only sink's diagnostics)."""
        return ProviderSinkDiagnostics(events_total=self._events)

    def handle(self, datum: MarketData) -> None:
        """Submit one event to M2 and record it in L1; raise on a terminal break (no Redis I/O).

        On acceptance the just-allocated producer_sequence (an O(1) read, no diagnostics build) is
        recorded as the L1 accepted position — distinct from the published position, which the
        bounded observer advances from confirmed M2/D1 completions.
        """
        self._events += 1
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
    redis: Redis

    async def aclose(self) -> None:
        """Close the owned Redis client (the final shutdown step).

        The composition root creates one Redis client per incarnation and hands it to this stack;
        the ingestion service closes it here *after* M2 has drained and L1 is finalised — never
        before, because the worker publishes through this client. redis-py's ``aclose`` is safe to
        call more than once.
        """
        await self.redis.aclose()


def build_publication_stack(
    *,
    redis: Redis,
    config: MarketIpcConfig,
    producer_id: str,
    state_dir: Path,
    now: Callable[[], datetime],
    trading_date_source: TradingDateSource,
    universe_version: int = 0,
) -> PublicationStack:
    """Construct the H3 publication stack over ``redis`` (real or test); no epoch allocated yet.

    The M1 epoch is allocated later, by ``boundary.start()`` → ``publisher.start()`` (frozen
    startup order); constructing the stack performs no Redis I/O and no epoch allocation. The
    ``redis`` client is owned by the returned stack and closed by :meth:`PublicationStack.aclose`
    on shutdown. ``trading_date_source`` is read per event (e.g. :class:`SessionTradingDate`), so a
    long-lived producer crosses trading-date boundaries without a restart.
    """
    stream = RedisMarketEventStream(redis=redis, config=config)
    atomic_publisher = RedisAtomicPublisher(redis, config)
    publisher = MarketEventPublisher(
        stream=stream,
        config=config,
        producer_id=producer_id,
        epoch_allocator=DurableEpochAllocator(state_dir),
        trading_date_source=trading_date_source,
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
        redis=redis,
    )
