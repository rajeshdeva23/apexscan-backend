"""Long-lived backend SHADOW consumer runtime + offline composition (DECOUPLING PHASE H4A).

Phase C built the shadow :class:`MarketEventConsumer` (read -> validate -> dedup -> decode ->
sink -> ACK) and C1 gave it a durable Redis dedup authority. Neither owns a process: there is no
Redis client lifecycle, no poll loop, and no readiness/shutdown discipline. H4A adds exactly that
composition boundary — a runtime that owns one Redis client, drives ``poll_once`` on a cancellable
background task, exposes bounded readiness, and tears everything down deterministically (task
cancelled, Redis closed exactly once).

Strictly NON-AUTHORITATIVE and OFFLINE. The only legal composition is the ADR-025
``SHADOW_CONSUME_COMPARE`` mode (``ipc_consumer_enabled`` + ``ipc_shadow_compare_enabled``, no
authority/publisher/producer); every other flag shape composes an inert (disabled) runtime. The
runtime never drives the TickEngine/EventBus/strategies/sector, never activates a Dhan provider or
the IPC publisher, and is not wired into backend startup — it is reachable only through this
explicit composition from a test or a standalone offline process. Durable idempotency comes from
the injected :class:`CompositeDeduplicator`; the residual apply->mark window (B2) is unresolved and
harmless only because the sink is non-authoritative.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from redis.asyncio import Redis

from app.market_ipc.consumer import (
    ConsumerDiagnostics,
    MarketEventConsumer,
    RecordingShadowSink,
)
from app.market_ipc.dedup import BoundedDeduplicator
from app.market_ipc.durable_dedup import CompositeDeduplicator, DurableDeduplicator
from app.market_ipc.health import IngestionHealthReader
from app.market_ipc.loss_detection import (
    LossDetectionResult,
    LossDetectionState,
    RedisLossDetector,
)
from app.market_ipc.reference import RedisCompactedReferenceStore, ReferenceStateLoader
from app.market_ipc.transport import RedisMarketEventStream

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from app.core.config import Settings
    from app.market_ingestion.mode import MarketPathMode, PhaseHFlags
    from app.market_ipc.consumer import (
        ShadowMarketEventSink,
        TradingDateAuthority,
        UniverseVersionAuthority,
    )
    from app.market_ipc.reference import ReferenceSnapshot

logger = logging.getLogger(__name__)

_ORIGIN_ID = "0-0"  # a stream id that has never had an entry generated (B11 fail-closed default)


class RuntimeState(StrEnum):
    """Consumer-runtime lifecycle state (READY only once the poll loop is running)."""

    DISABLED = "disabled"
    NOT_STARTED = "not_started"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


class MarketEventConsumerRuntime:
    """Owns one Redis client + the shadow consumer + its cancellable poll loop.

    Constructed only by :func:`compose_consumer_runtime` (offline) or tests. A ``consumer`` of
    ``None`` is the inert/disabled runtime (no Redis, no task) for any non-shadow flag shape.
    """

    def __init__(
        self,
        *,
        mode: MarketPathMode,
        flags: PhaseHFlags,
        consumer: MarketEventConsumer | None = None,
        redis: Redis | None = None,
        poll_idle_seconds: float = 0.0,
        loss_detector: RedisLossDetector | None = None,
        health_reader: IngestionHealthReader | None = None,
        reference_loader: ReferenceStateLoader | None = None,
        trading_date_source: TradingDateAuthority | None = None,
        universe_version_source: UniverseVersionAuthority | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._mode = mode
        self._flags = flags
        self._consumer = consumer
        self._redis = redis
        self._poll_idle_seconds = poll_idle_seconds
        self._loss_detector = loss_detector
        self._health_reader = health_reader
        self._reference_loader = reference_loader
        self._trading_date_source = trading_date_source
        self._universe_version_source = universe_version_source
        self._reference_snapshot: ReferenceSnapshot | None = None
        self._now = now or _utc_now
        self._state = RuntimeState.DISABLED if consumer is None else RuntimeState.NOT_STARTED
        self._task: asyncio.Task[None] | None = None
        self._redis_closed = False

    @property
    def mode(self) -> MarketPathMode:
        """The ADR-025 market-path mode this runtime was composed for."""
        return self._mode

    @property
    def state(self) -> RuntimeState:
        """Current lifecycle state."""
        return self._state

    @property
    def enabled(self) -> bool:
        """Whether this runtime owns a live consumer (vs the inert/disabled shape)."""
        return self._consumer is not None

    @property
    def is_ready(self) -> bool:
        """Readiness is the running poll loop — never merely that the process exists (§35)."""
        return self._state is RuntimeState.RUNNING

    async def start(self) -> None:
        """Ensure the group, then launch the poll loop; fail closed with no leaked task/client.

        A disabled runtime is a no-op. On a startup failure (e.g. Redis unreachable so the group
        cannot be ensured) the owned Redis client is closed and the state is FAILED before the
        error propagates — the runtime never claims READY on a partial start (§19).
        """
        if self._consumer is None:
            self._state = RuntimeState.DISABLED
            return
        if self._state in (RuntimeState.STARTING, RuntimeState.RUNNING):
            return  # already starting/running: never double-launch the poll task
        self._state = RuntimeState.STARTING
        try:
            await self._bootstrap_reference()  # seed durable reference BEFORE any event applies
            # Idempotent XGROUP CREATE (mkstream); raises if Redis is unreachable.
            await self._consumer.start()
        except BaseException:
            self._state = RuntimeState.FAILED
            await self._close_redis()  # unwind: never leave the owned client open on a failed start
            raise
        self._task = asyncio.create_task(self._run())
        self._task.add_done_callback(self._on_task_done)
        self._state = RuntimeState.RUNNING

    async def _bootstrap_reference(self) -> None:
        """Recover durable session reference from Redis and seed the sink, before events apply.

        The whole point (Gate D) is to rehydrate ``previous_close`` / session OHLC from the
        compacted ``md:reference:<date>`` hash WITHOUT re-authenticating to Dhan. It reads only that
        hash — a key wholly separate from the stream, group, and PEL — so it never ACKs an entry,
        discards a pending entry, resets the group, or rewinds durable consumer progress. A missing
        trading-date authority yields no bootstrap (warming up); a Redis outage propagates so the
        start fails closed rather than mistaking an outage for an empty session.
        """
        if self._reference_loader is None or self._trading_date_source is None:
            return
        trading_date = self._trading_date_source()
        if trading_date is None:
            return  # no authoritative date yet: warming up, seed nothing
        expected_version = (
            self._universe_version_source() if self._universe_version_source is not None else None
        )
        snapshot = await self._reference_loader.load(trading_date, expected_version)
        self._reference_snapshot = snapshot
        seed = getattr(self._consumer.sink, "seed_reference", None) if self._consumer else None
        if seed is not None:
            await seed(snapshot)

    async def _run(self) -> None:
        """Drive bounded poll cycles until stopped; ``poll_once`` never raises (faults counted).

        With ``block_ms > 0`` the XREADGROUP long-poll paces the loop; with ``block_ms == 0`` the
        idle sleep prevents a busy loop. The sleep is unconditional so every cycle yields — a zero
        idle still hands control back so ``stop`` can cancel a non-blocking loop. Cancellation wakes
        a blocked read and ends the loop cleanly.
        """
        assert self._consumer is not None
        while self._state is RuntimeState.RUNNING:
            await self._consumer.poll_once()
            await asyncio.sleep(self._poll_idle_seconds)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        """Flip to FAILED if the poll loop ends abnormally so readiness never lies (§35)."""
        if task.cancelled() or self._state is not RuntimeState.RUNNING:
            return
        if task.exception() is not None:
            self._state = RuntimeState.FAILED

    async def stop(self) -> None:
        """Stop the loop, cancel a blocked read, and close the Redis client exactly once.

        Idempotent and safe from any state. ``poll_once`` processes each entry to a terminal
        outcome before the next, so cancelling between entries never ACKs a partially processed
        event — an un-ACKed entry simply redelivers (§36). The Redis client is closed even if the
        loop task ended abnormally, so an unexpected loop fault can never leak the owned client.
        """
        if self._consumer is None:
            self._state = RuntimeState.DISABLED
            return
        self._state = RuntimeState.STOPPING
        try:
            if self._task is not None:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._task
                self._task = None
        finally:
            await self._close_redis()
            self._state = RuntimeState.STOPPED

    async def _close_redis(self) -> None:
        """Close the owned Redis client at most once (idempotent; a close fault is swallowed)."""
        if self._redis is None or self._redis_closed:
            return
        self._redis_closed = True
        with contextlib.suppress(Exception):
            await self._redis.aclose()

    def diagnostics(self) -> ConsumerDiagnostics | None:
        """Bounded consumer counters, or ``None`` for the inert/disabled runtime."""
        return self._consumer.diagnostics() if self._consumer is not None else None

    @property
    def reference_snapshot(self) -> ReferenceSnapshot | None:
        """The reference state recovered at bootstrap, or ``None`` if none was loaded (Gate D)."""
        return self._reference_snapshot

    async def evaluate_authority_readiness(self) -> LossDetectionResult:
        """Reconcile transport continuity now (B11 composed) as an authority-readiness INPUT.

        Feeds the loss detector three evidence sources: the producer L1 position from ``md:health``
        (staleness-gated), the consumer's durably-applied ``(epoch, sequence)``, and bounded Redis
        metadata. Fails closed to ``INSUFFICIENT_EVIDENCE`` (``ready_for_authority=False``) when the
        runtime is inert or the producer health snapshot is missing/stale — a missing producer
        position is never read as healthy. This activates NOTHING (H9B); Phase H9C owns authority.
        """
        if self._consumer is None or self._loss_detector is None or self._health_reader is None:
            return self._unavailable("consumer runtime is not composed for loss detection")
        producer = await self._health_reader.read_evidence(self._now())
        if producer is None:
            return self._unavailable("producer md:health snapshot is missing or stale")
        return await self._loss_detector.evaluate(producer, self._consumer.consumer_progress())

    def _unavailable(self, reason: str) -> LossDetectionResult:
        """Build a fail-closed INSUFFICIENT_EVIDENCE result when producer evidence is missing."""
        consumer = self._consumer.consumer_progress() if self._consumer is not None else None
        return LossDetectionResult(
            state=LossDetectionState.INSUFFICIENT_EVIDENCE,
            reason=reason,
            ready_for_authority=False,
            producer_id="unknown",
            producer_epoch=0,
            producer_last_published_sequence=None,
            consumer_last_applied_sequence=(
                consumer.last_applied_sequence if consumer is not None else None
            ),
            stream_length=0,
            stream_last_generated_id=_ORIGIN_ID,
            group_last_delivered_id=_ORIGIN_ID,
            pending=0,
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def compose_consumer_runtime(
    settings: Settings,
    *,
    sink: ShadowMarketEventSink | None = None,
    trading_date_source: TradingDateAuthority | None = None,
    universe_version_source: UniverseVersionAuthority | None = None,
    now: Callable[[], datetime] | None = None,
) -> MarketEventConsumerRuntime:
    """Build the shadow consumer runtime from settings (offline; never wired into startup).

    ``SHADOW_CONSUME_COMPARE`` composes a live runtime over ``Redis.from_url(settings.redis_url)``
    with a durable :class:`CompositeDeduplicator`; every other legal flag shape returns an inert
    runtime that owns no Redis client. The trading-date/universe authorities default to ``None``
    (no live authority is wired in H4A: a ``None`` universe authority means UNKNOWN, so the
    composed-but-unwired shadow applies nothing until reconciliation is connected in a later
    phase); tests inject concrete sources to exercise the apply/idempotency path.
    """
    from app.market_ingestion.mode import (  # lazy: keep market_ipc import-neutral / cycle-free
        MarketPathMode,
        derive_market_path_mode,
    )

    flags = settings.phase_h_flags()
    mode = derive_market_path_mode(flags)  # validates the ADR-025 matrix (raises on illegal shapes)
    if mode is not MarketPathMode.SHADOW_CONSUME_COMPARE:
        return MarketEventConsumerRuntime(mode=mode, flags=flags)

    config = settings.market_ipc_config()
    redis: Redis = Redis.from_url(settings.redis_url)
    deduplicator = CompositeDeduplicator(
        memory=BoundedDeduplicator(config.dedup_max_entries),
        durable=DurableDeduplicator(redis, config),
    )
    clock = now or _utc_now
    consumer = MarketEventConsumer(
        transport=RedisMarketEventStream(redis=redis, config=config),
        config=config,
        sink=sink or RecordingShadowSink(),
        trading_date_source=trading_date_source or (lambda: None),
        universe_version_source=universe_version_source or (lambda: None),
        now=clock,
        deduplicator=deduplicator,
    )
    reference_store = RedisCompactedReferenceStore(redis, config)
    return MarketEventConsumerRuntime(
        mode=mode,
        flags=flags,
        consumer=consumer,
        redis=redis,
        poll_idle_seconds=0.0 if config.block_ms > 0 else 0.05,
        loss_detector=RedisLossDetector(redis, config),
        health_reader=IngestionHealthReader(redis, config),
        reference_loader=ReferenceStateLoader(source=reference_store, now=clock),
        trading_date_source=trading_date_source,
        universe_version_source=universe_version_source,
        now=clock,
    )
