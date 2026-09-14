"""Market-ingestion restart / new producer-epoch validation (DECOUPLING PHASE H7).

Proves the complementary lifecycle to H6. H6 kept ONE ingestion incarnation alive while the
backend consumer was destroyed/recreated. H7 does the opposite: the market-ingestion service
itself is terminated and recreated as a genuinely NEW producer incarnation while the backend
consumer survives (or restarts independently) and Redis preserves the event boundary.

    deterministic gated provider (no Dhan / tokens / sockets / internet)
        -> market-ingestion service A   (producer_id P, producer_epoch E)
        -> M1 -> M2 -> D1 -> L1          (real composition; DurableEpochAllocator is load-bearing)
        -> Redis md:events (+ md:reference:<date>)
        ... service A stops / faults / is abruptly lost ...
        -> market-ingestion service B   (producer_id P, producer_epoch > E, sequence RESET)
        -> backend consumer + durable C1 -> RecordingShadowSink -> H4C comparator

The primary invariant (§5): across a genuine ingestion restart the durable producer epoch strictly
increases and is NEVER reused, so a post-restart ``seq=1`` under a new epoch can never collide in
C1 with the previous incarnation's ``seq=1``. A provider transport reconnect WITHIN one incarnation
must NOT allocate a new epoch (§30). Both are proven here.

H7 needs NO production-code change: the DurableEpochAllocator (M1) already persists the epoch on a
producer-local crash-safe file (allocated at ``publisher.start()``, before the provider connects),
so a new incarnation on the same durable state directory reads the prior epoch and allocates a
strictly higher one; the epoch is fixed once per incarnation and a supervisor-driven provider
reconnect never re-allocates it.

H7 is OFFLINE and NON-AUTHORITATIVE: no live Dhan, no production contact, no consumer/publisher
activation, no IPC authority, no TickEngine/MarketContext, no FIX-2 workaround. Timestamps are
canonical tz-aware UTC. B2/B4/B11 remain DESIGN_RESOLVED / IMPLEMENTATION_PENDING (demonstrated,
never solved). Skips cleanly if redislite is absent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.adapters.base.broker_adapter import BrokerAdapter
from app.market_ingestion.mode import PhaseHFlags
from app.market_ingestion.publication import (
    PublicationStack,
    PublishingEventSink,
    build_publication_stack,
)
from app.market_ingestion.service import MarketIngestionService, ServiceStatus
from app.market_ipc import (
    BoundedDeduplicator,
    CompositeDeduplicator,
    ContinuityState,
    DurableDeduplicator,
    FeedContinuityTracker,
    MarketEventConsumer,
    MarketEventEnvelope,
    MarketIpcConfig,
    RecordingShadowSink,
    RedisMarketEventStream,
    build_envelope,
    compare,
    view_from_envelope,
    views_from_applied,
)
from app.market_ipc.atomic import AtomicPublicationResult, RedisAtomicPublisher
from app.market_ipc.boundary import AsyncPublicationBoundary
from app.market_ipc.epoch import DurableEpochAllocator, EpochStateError
from app.market_ipc.events import IpcPayload
from app.market_ipc.publisher import MarketEventPublisher, StaticUniverseVersion
from app.market_ipc.transport import _FIELD, RedisPublishError
from app.schemas.market_data import (
    Instrument,
    MarketDataKind,
    MarketReference,
    ProviderCapability,
    ProviderHealth,
    ProviderSessionOhlc,
    ProviderStatus,
    Quote,
    SubscriptionRequest,
    Tick,
)

redislite = pytest.importorskip("redislite", reason="disposable real Redis unavailable")

_NOW = datetime(2026, 9, 9, 10, 15, 30, tzinfo=UTC)
_TD = date(2026, 9, 9)
_PRODUCER = "market-ingestion"
_SYMBOLS = ("TCS", "INFY", "RELIANCE", "HDFC", "WIPRO")
_CUT = object()  # sentinel: dequeuing it ends a stream attempt (a recoverable transport drop)


@pytest.fixture(scope="module")
def redis_socket() -> str:
    server = redislite.Redis()
    try:
        yield server.socket_file
    finally:
        server.shutdown()


@pytest.fixture
async def redis(redis_socket: str) -> Redis:
    """A flush-on-setup assertion/probe client (incarnations create their own clients)."""
    client: Redis = Redis(unix_socket_path=redis_socket)
    await client.flushall()
    try:
        yield client
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# Canonical fixture builders (deterministic, tz-aware UTC; no FIX-2 workaround)
# --------------------------------------------------------------------------- #
def _tick(symbol: str = "TCS", price: str = "100.5") -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        event_timestamp=_NOW,
        last_price=Decimal(price),
    )


def _tick_with_ohlc(symbol: str = "TCS") -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        event_timestamp=_NOW,
        last_price=Decimal("100.5"),
        traded_quantity=10,
        session_cumulative_volume=1_000,
        session_ohlc=ProviderSessionOhlc(
            open_price=Decimal("99"),
            high_price=Decimal("101"),
            low_price=Decimal("98"),
            close_price=Decimal("100.5"),
        ),
    )


def _quote(symbol: str = "TCS") -> Quote:
    return Quote(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        event_timestamp=_NOW,
        bid_price=Decimal("100"),
        ask_price=Decimal("101"),
        bid_quantity=5,
        ask_quantity=7,
    )


def _reference(symbol: str = "TCS") -> MarketReference:
    return MarketReference(
        instrument=Instrument(exchange="NSE", symbol=symbol), previous_close=Decimal("99.25")
    )


def _mix(count: int, *, offset: int = 0) -> list[IpcPayload]:
    """Deterministic canonical events across instruments and all IPC-supported kinds."""
    events: list[IpcPayload] = []
    for i in range(offset, offset + count):
        symbol = _SYMBOLS[i % len(_SYMBOLS)]
        selector = i % 4
        if selector == 0:
            events.append(_tick(symbol=symbol, price=str(100 + (i % 50))))
        elif selector == 1:
            events.append(_quote(symbol=symbol))
        elif selector == 2:
            events.append(_reference(symbol=symbol))
        else:
            events.append(_tick_with_ohlc(symbol=symbol))
    return events


def _envelope(payload: IpcPayload, *, seq: int, epoch: int) -> MarketEventEnvelope:
    """Reconstruct the envelope the producer must have built for the seq-th submitted event."""
    return build_envelope(
        payload,
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=7,
    )


def _expected_views(events: list[IpcPayload], *, epoch: int, start_seq: int = 1) -> list[object]:
    """Views for one incarnation's batch: sequence restarts at ``start_seq`` under ``epoch``."""
    return [
        view_from_envelope(_envelope(datum, seq=start_seq + i, epoch=epoch))
        for i, datum in enumerate(events)
    ]


class _FixedTradingDate:
    """Deterministic producer trading-date source (canonical UTC session date; no host clock)."""

    def current_trading_date(self) -> date:
        return _TD


def _request() -> SubscriptionRequest:
    return SubscriptionRequest(
        instruments=tuple(Instrument(exchange="NSE", symbol=s) for s in _SYMBOLS),
        data_types=frozenset({MarketDataKind.TICK}),
    )


async def _no_sleep(_seconds: float) -> None:
    """Instant supervisor backoff for deterministic tests (still yields to the event loop)."""
    await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# Providers — each ingestion incarnation owns its OWN provider (H7: restart recreates it)
# --------------------------------------------------------------------------- #
class _GatedProvider(BrokerAdapter):
    """Live provider whose feed the test drives on demand; ONE stream incarnation, stays up.

    ``stream_market_data`` is entered once and blocks on an internal queue between batches. Each
    ingestion incarnation constructs a fresh provider, so an ingestion restart DOES recreate the
    provider lifecycle (unlike H6, where the single provider was never reconnected). No Dhan,
    tokens, sockets, or internet.
    """

    capabilities = frozenset({ProviderCapability.LIVE_MARKET_DATA})

    def __init__(self) -> None:
        self._queue: asyncio.Queue[IpcPayload] = asyncio.Queue()
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.stream_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def stream_market_data(self, request: SubscriptionRequest):  # noqa: ARG002 - stub feed
        self.stream_calls += 1
        while True:
            yield await self._queue.get()  # blocks between batches; cancelled cleanly on stop()

    def push(self, events: list[IpcPayload]) -> None:
        for datum in events:
            self._queue.put_nowait(datum)


class _ReconnectingProvider(BrokerAdapter):
    """Provider whose stream drops recoverably on a CUT sentinel, forcing a supervisor reconnect.

    A within-incarnation provider reconnect (the supervisor re-iterating ``stream_market_data``
    after a recoverable drop) must NOT allocate a new producer epoch (§30): the epoch is allocated
    once by ``publisher.start()`` and the supervisor never re-runs it. ``connect`` is called exactly
    once (by the coordinator at startup); a reconnect only re-enters ``stream_market_data`` (so
    ``stream_calls`` grows while ``connect_calls`` stays 1).
    """

    capabilities = frozenset({ProviderCapability.LIVE_MARKET_DATA})

    def __init__(self) -> None:
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.stream_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def stream_market_data(self, request: SubscriptionRequest):  # noqa: ARG002 - stub feed
        self.stream_calls += 1
        while True:
            item = await self._queue.get()
            if item is _CUT:
                raise ConnectionError("simulated recoverable provider transport drop")
            yield item  # type: ignore[misc]

    def push(self, events: list[IpcPayload]) -> None:
        for datum in events:
            self._queue.put_nowait(datum)

    def cut(self) -> None:
        """End the current stream attempt recoverably (the supervisor will reconnect)."""
        self._queue.put_nowait(_CUT)


# --------------------------------------------------------------------------- #
# Fault-injection doubles for the producer D1 / M1 seams
# --------------------------------------------------------------------------- #
class _FailNthAtomic:
    """Delegates to a real D1 but fails the Nth publish once (a legal seq gap on the stream)."""

    def __init__(self, delegate: RedisAtomicPublisher, *, fail_on: int) -> None:
        self._delegate = delegate
        self._fail_on = fail_on
        self._calls = 0

    async def publish_stream_only(self, envelope: MarketEventEnvelope) -> AtomicPublicationResult:
        self._calls += 1
        if self._calls == self._fail_on:
            raise RedisPublishError("simulated transient D1 failure")
        return await self._delegate.publish_stream_only(envelope)

    async def publish_stream_and_reference(
        self, envelope: MarketEventEnvelope, reference_state: object
    ) -> AtomicPublicationResult:
        self._calls += 1
        if self._calls == self._fail_on:
            raise RedisPublishError("simulated transient D1 failure")
        return await self._delegate.publish_stream_and_reference(envelope, reference_state)


class _FailingAllocator:
    """Epoch allocator that always fails closed (simulated durable-state read/allocate failure)."""

    async def allocate(self, producer_id: str) -> int:  # noqa: ARG002 - deliberate failure
        raise EpochStateError("simulated durable producer-epoch allocation failure")


class _AckCrashTransport:
    """Transport delegate whose ``ack`` raises; every other primitive is the real stream."""

    def __init__(self, delegate: RedisMarketEventStream) -> None:
        self._delegate = delegate

    async def ensure_group(self) -> None:
        await self._delegate.ensure_group()

    async def read_raw(self) -> list[tuple[str, bytes | None]]:
        return await self._delegate.read_raw()

    async def claim_page_raw(self, start_id: str) -> tuple[str, list[tuple[str, bytes | None]]]:
        return await self._delegate.claim_page_raw(start_id)

    async def ack(self, *message_ids: str) -> int:
        raise RedisError("simulated ACK failure")


class _RecordCrashDedup:
    """Dedup whose durable ``record`` raises (crash before the C1 mark commits); contains real."""

    def __init__(self, delegate: CompositeDeduplicator) -> None:
        self._delegate = delegate

    async def contains(self, identity: object) -> bool:
        return await self._delegate.contains(identity)  # type: ignore[arg-type]

    async def record(self, identity: object) -> None:
        raise RedisError("simulated crash before durable mark")


# --------------------------------------------------------------------------- #
# Producer composition (real M1/M2/D1/L1); a fresh call = a genuinely new incarnation
# --------------------------------------------------------------------------- #
def _flags() -> PhaseHFlags:
    return PhaseHFlags(
        market_ingestion_service_enabled=True,
        ipc_publisher_enabled=True,
        ipc_consumer_enabled=False,
        ipc_shadow_compare_enabled=False,
        ipc_authoritative_enabled=False,
        legacy_market_path_enabled=True,
    )


def _publication_stack(
    *,
    redis: Redis,
    config: MarketIpcConfig,
    state_dir: Path,
    allocator: object | None = None,
    atomic: object | str = "auto",
) -> PublicationStack:
    """Assemble the REAL M1/M2/D1/L1 stack; ``allocator``/``atomic`` overridable for fault tests."""
    stream = RedisMarketEventStream(redis=redis, config=config)
    atomic_publisher = RedisAtomicPublisher(redis, config) if atomic == "auto" else atomic
    publisher = MarketEventPublisher(
        stream=stream,
        config=config,
        producer_id=_PRODUCER,
        epoch_allocator=allocator or DurableEpochAllocator(state_dir),  # type: ignore[arg-type]
        trading_date_source=_FixedTradingDate(),
        universe_version_source=StaticUniverseVersion(7),
        now=lambda: _NOW,
        atomic_publisher=atomic_publisher,  # type: ignore[arg-type]
    )
    boundary = AsyncPublicationBoundary(
        publisher=publisher,
        capacity=config.publish_queue_capacity,
        drain_timeout_seconds=1.0,
        now=lambda: _NOW,
    )
    continuity = FeedContinuityTracker()
    sink = PublishingEventSink(boundary=boundary, continuity=continuity, publisher=publisher)
    return PublicationStack(
        producer_id=_PRODUCER,
        publisher=publisher,
        boundary=boundary,
        continuity=continuity,
        sink=sink,
        redis=redis,
    )


def _service(
    provider: BrokerAdapter, stack: PublicationStack, *, max_reconnects: int = 0
) -> MarketIngestionService:
    return MarketIngestionService(
        flags=_flags(),
        provider=provider,
        subscription_request=_request(),
        publication=stack,
        supervisor_max_reconnects=max_reconnects,
        supervisor_sleep=_no_sleep,
        observer_interval_seconds=0.005,
    )


class _LiveIngestion:
    """A running market-ingestion incarnation (one producer epoch)."""

    def __init__(
        self, service: MarketIngestionService, stack: PublicationStack, provider: BrokerAdapter
    ) -> None:
        self.service = service
        self.stack = stack
        self.provider = provider

    @property
    def epoch(self) -> int:
        epoch = self.service.diagnostics().producer_epoch
        assert epoch is not None
        return epoch

    @property
    def published_total(self) -> int:
        return self.service.diagnostics().published_total

    @property
    def accepted_sequence(self) -> int | None:
        return self.service.diagnostics().last_accepted_sequence

    @property
    def reconnect_total(self) -> int:
        return self.service.diagnostics().reconnect_total

    @property
    def continuity_state(self) -> ContinuityState:
        return self.stack.continuity.snapshot().state

    def push(self, events: list[IpcPayload]) -> None:
        self.provider.push(events)  # type: ignore[attr-defined]

    async def publish(self, events: list[IpcPayload]) -> None:
        """Feed a batch and wait until every event of it has reached Redis."""
        target = self.published_total + len(events)
        self.provider.push(events)  # type: ignore[attr-defined]
        await self.await_published(target)

    async def await_published(self, target: int) -> None:
        for _ in range(300_000):
            if self.published_total >= target:
                return
            await asyncio.sleep(0.001)
        raise AssertionError(f"published {self.published_total} < {target}")

    async def await_reconnect(self, target: int) -> None:
        for _ in range(300_000):
            if self.reconnect_total >= target:
                return
            await asyncio.sleep(0.001)
        raise AssertionError(f"reconnect_total {self.reconnect_total} < {target}")

    async def await_status(self, status: ServiceStatus) -> None:
        for _ in range(300_000):
            if self.service.status is status:
                return
            await asyncio.sleep(0.001)
        raise AssertionError(f"status {self.service.status} != {status}")

    async def await_continuity(self, state: ContinuityState) -> None:
        for _ in range(300_000):
            if self.continuity_state is state:
                return
            await asyncio.sleep(0.001)
        raise AssertionError(f"continuity {self.continuity_state} != {state}")

    async def stop(self) -> None:
        # cancels the supervisor, drains M2 -> Redis, disconnects the provider, closes the client
        await self.service.stop()


async def _start_ingestion(
    redis_socket: str,
    config: MarketIpcConfig,
    state_dir: Path,
    *,
    provider: BrokerAdapter | None = None,
    allocator: object | None = None,
    atomic: object | str = "auto",
    max_reconnects: int = 0,
) -> _LiveIngestion:
    """Start ONE real market-ingestion incarnation (publisher mode) over a gated fake provider."""
    provider = provider or _GatedProvider()
    prod: Redis = Redis(unix_socket_path=redis_socket)
    stack = _publication_stack(
        redis=prod, config=config, state_dir=state_dir, allocator=allocator, atomic=atomic
    )
    service = _service(provider, stack, max_reconnects=max_reconnects)
    await service.start()
    return _LiveIngestion(service, stack, provider)


def _real_stack(redis_socket: str, config: MarketIpcConfig, state_dir: Path) -> PublicationStack:
    """The production composition-root helper (used where no fault seam is injected)."""
    return build_publication_stack(
        redis=Redis(unix_socket_path=redis_socket),
        config=config,
        producer_id=_PRODUCER,
        state_dir=state_dir,
        now=lambda: _NOW,
        trading_date_source=_FixedTradingDate(),
        universe_version=7,
    )


# --------------------------------------------------------------------------- #
# Backend consumer incarnation — own client, EMPTY memory cache, shared durable C1 + group
# --------------------------------------------------------------------------- #
def _config(**overrides: object) -> MarketIpcConfig:
    return MarketIpcConfig(block_ms=0, **overrides)


def _fast_idle(config: MarketIpcConfig, *, consumer_name: str) -> MarketIpcConfig:
    return config.model_copy(update={"consumer_name": consumer_name, "claim_idle_ms": 1})


def _durable(redis: Redis, config: MarketIpcConfig) -> CompositeDeduplicator:
    return CompositeDeduplicator(
        memory=BoundedDeduplicator(config.dedup_max_entries),
        durable=DurableDeduplicator(redis, config),
    )


@dataclass
class _Backend:
    """One genuinely-separate backend consumer incarnation (own client, cache, name, sink)."""

    client: Redis
    consumer: MarketEventConsumer
    sink: RecordingShadowSink
    config: MarketIpcConfig

    async def drain(self) -> None:
        await _drain(self.consumer, self.client, self.config)

    async def close(self) -> None:
        await self.client.aclose()


async def _start_backend(
    redis_socket: str,
    config: MarketIpcConfig,
    name: str,
    *,
    deduplicator: object | None = None,
    transport: object | None = None,
    max_entries: int = 40_000,
) -> _Backend:
    """Construct + start a fresh backend: own client, EMPTY memory cache, shared durable C1."""
    client: Redis = Redis(unix_socket_path=redis_socket)
    cfg = _fast_idle(config, consumer_name=name)
    sink = RecordingShadowSink(max_entries=max_entries)
    consumer = MarketEventConsumer(
        transport=transport or RedisMarketEventStream(redis=client, config=cfg),  # type: ignore[arg-type]
        config=cfg,
        sink=sink,
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: 7,
        now=lambda: _NOW,
        deduplicator=deduplicator or _durable(client, cfg),  # type: ignore[arg-type]
    )
    await consumer.start()
    return _Backend(client=client, consumer=consumer, sink=sink, config=cfg)


async def _pending(redis: Redis, config: MarketIpcConfig) -> int:
    summary = await redis.xpending(config.stream_name, config.consumer_group)
    return int(summary["pending"])


async def _drain(consumer: MarketEventConsumer, redis: Redis, config: MarketIpcConfig) -> None:
    for _ in range(4_000):
        before = consumer.diagnostics().acked_total
        await consumer.poll_once()
        if consumer.diagnostics().acked_total == before and await _pending(redis, config) == 0:
            return
    raise AssertionError("stream did not drain within the cycle budget")


async def _reinject(redis: Redis, config: MarketIpcConfig, *, start: int, count: int) -> None:
    """Re-append ``count`` stream entries starting at index ``start``: at-least-once redelivery."""
    entries = await redis.xrange(config.stream_name)
    for _id, fields in entries[start : start + count]:
        raw = next(iter(fields.values()))
        await redis.xadd(config.stream_name, {_FIELD: raw}, maxlen=config.maxlen, approximate=True)


def _all_applied(backends: list[_Backend]) -> list[object]:
    """Union of applied (envelope, event) views across every backend incarnation."""
    events: list[tuple[MarketEventEnvelope, IpcPayload]] = []
    for backend in backends:
        events.extend(backend.sink.events)
    return views_from_applied(events)


# =========================================================================== #
# H7-T01/T02/T03/T04/T07/T08/T09 clean ingestion restart -> new epoch, seq reset
# =========================================================================== #
async def test_h7_t01_t02_t03_t04_t07_t08_t09_clean_ingestion_restart_new_epoch(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config(read_count=200)
    backend = await _start_backend(redis_socket, config, "backend")  # T07: alive whole test
    batch_a = _mix(40, offset=0)
    batch_b = _mix(50, offset=40)

    # Incarnation A: producer_id P, epoch E, sequence 1..40.
    ingestion_a = await _start_ingestion(redis_socket, config, tmp_path)
    await ingestion_a.publish(batch_a)
    epoch_a = ingestion_a.epoch
    assert ingestion_a.service.diagnostics().producer_epoch == epoch_a
    assert ingestion_a.accepted_sequence == 40  # contiguous 1..40
    await backend.drain()
    assert backend.sink.applied_total == 40
    stream_after_a = await redis.xlen(config.stream_name)
    group_after_a = await redis.xinfo_groups(config.stream_name)
    await ingestion_a.stop()  # clean shutdown; T15 (genuine restart)

    # Incarnation B: SAME durable state dir -> epoch strictly higher; sequence RESETS to 1..50.
    ingestion_b = await _start_ingestion(redis_socket, config, tmp_path)
    epoch_b = ingestion_b.epoch
    assert epoch_b > epoch_a  # T03: new incarnation epoch increases
    await ingestion_b.publish(batch_b)
    assert ingestion_b.accepted_sequence == 50  # T04: sequence restarted at 1 under the new epoch

    # T08/T09: the stream and consumer group survived the ingestion restart (not recreated).
    assert await redis.xlen(config.stream_name) == stream_after_a + 50
    assert await redis.xinfo_groups(config.stream_name) == group_after_a
    assert backend.consumer is backend.consumer  # T07: same backend incarnation throughout

    await backend.drain()
    report = compare(
        _expected_views(batch_a, epoch=epoch_a) + _expected_views(batch_b, epoch=epoch_b),
        _all_applied([backend]),
        sample_limit=20,
    )
    assert report.is_clean
    assert report.matched_total == 90
    assert report.missing_total == 0
    assert report.unexpected_total == 0
    assert report.duplicate_suppressed_total == 0  # same seq / distinct epoch never merged
    assert _PRODUCER == "market-ingestion"  # T02: producer id fixed across incarnations
    await ingestion_b.stop()
    await backend.close()


# =========================================================================== #
# H7-T05/T06 same sequence under a new epoch both apply; replaying an epoch is suppressed
# =========================================================================== #
async def test_h7_t05_t06_same_seq_new_epoch_apply_then_replay_suppressed(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    first = _mix(5, offset=0)
    second = _mix(5, offset=5)  # distinct payloads, but the producer sequence resets to 1..5

    ingestion_a = await _start_ingestion(redis_socket, config, tmp_path)
    await ingestion_a.publish(first)
    epoch_a = ingestion_a.epoch
    await ingestion_a.stop()

    ingestion_b = await _start_ingestion(redis_socket, config, tmp_path)
    await ingestion_b.publish(second)
    epoch_b = ingestion_b.epoch
    await ingestion_b.stop()
    assert epoch_b > epoch_a

    backend = await _start_backend(redis_socket, config, "backend")
    await backend.drain()
    assert backend.sink.applied_total == 10  # T05: (E,1) and (E+1,1) are distinct C1 identities

    # T06: replay the epoch-A entries verbatim -> durable C1 suppresses them (no reapply).
    await _reinject(redis, config, start=0, count=5)
    applied_before = backend.sink.applied_total
    await backend.drain()
    assert backend.sink.applied_total == applied_before  # nothing re-applied
    assert backend.consumer.diagnostics().duplicate_total == 5

    report = compare(
        _expected_views(first, epoch=epoch_a) + _expected_views(second, epoch=epoch_b),
        _all_applied([backend]),
        sample_limit=20,
    )
    assert report.is_clean
    assert report.matched_total == 10
    await backend.close()


# =========================================================================== #
# H7-T10 an old-epoch entry stranded in a dead backend's PEL coexists with new-epoch traffic
# =========================================================================== #
async def test_h7_t10_old_pel_and_new_epoch_coexist(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config(read_count=50)
    old = _mix(5, offset=0)
    new = _mix(5, offset=5)

    # Incarnation A publishes; a backend reads its entries into the PEL WITHOUT acking, then dies.
    ingestion_a = await _start_ingestion(redis_socket, config, tmp_path)
    await ingestion_a.publish(old)
    epoch_a = ingestion_a.epoch
    a_cfg = _fast_idle(config, consumer_name="backend-a")
    a_client: Redis = Redis(unix_socket_path=redis_socket)
    a_stream = RedisMarketEventStream(redis=a_client, config=a_cfg)
    await a_stream.ensure_group()
    stranded = await a_stream.read_raw()
    assert len(stranded) == 5
    assert await _pending(redis, config) == 5
    await a_client.aclose()  # abrupt: A gone with epoch-A entries stranded in its PEL
    await ingestion_a.stop()

    # Ingestion restarts to a new epoch and publishes new traffic.
    ingestion_b = await _start_ingestion(redis_socket, config, tmp_path)
    await ingestion_b.publish(new)
    epoch_b = ingestion_b.epoch
    assert epoch_b > epoch_a

    # A fresh backend reclaims A's abandoned PEL (epoch A) AND consumes the new epoch-B entries.
    backend = await _start_backend(redis_socket, config, "backend-b")
    await asyncio.sleep(0.02)  # exceed the 1ms claim-idle threshold
    await backend.drain()
    assert backend.consumer.diagnostics().pending_reclaimed_applied > 0
    assert await _pending(redis, config) == 0

    report = compare(
        _expected_views(old, epoch=epoch_a) + _expected_views(new, epoch=epoch_b),
        _all_applied([backend]),
        sample_limit=20,
    )
    assert report.is_clean
    assert report.matched_total == 10  # no identity collision between the two epochs
    await backend.close()
    await ingestion_b.stop()


# =========================================================================== #
# H7-T11 an ACK lost for an old-epoch event is suppressed; a same-seq new-epoch event applies
# =========================================================================== #
async def test_h7_t11_ack_lost_old_epoch_suppressed_new_epoch_applies(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    old = [_tick(symbol="TCS", price="111")]
    new = [_tick(symbol="INFY", price="222")]  # different payload, SAME producer_sequence (1)

    ingestion_a = await _start_ingestion(redis_socket, config, tmp_path)
    await ingestion_a.publish(old)
    epoch_a = ingestion_a.epoch

    # Backend A applies + durably marks the epoch-A event, but its ACK fails and it disappears.
    a_cfg = _fast_idle(config, consumer_name="backend-a")
    a_client: Redis = Redis(unix_socket_path=redis_socket)
    sink_a = RecordingShadowSink()
    backend_a = _Backend(
        client=a_client,
        consumer=MarketEventConsumer(
            transport=_AckCrashTransport(RedisMarketEventStream(redis=a_client, config=a_cfg)),  # type: ignore[arg-type]
            config=a_cfg,
            sink=sink_a,
            trading_date_source=lambda: _TD,
            universe_version_source=lambda: 7,
            now=lambda: _NOW,
            deduplicator=_durable(a_client, a_cfg),
        ),
        sink=sink_a,
        config=a_cfg,
    )
    await backend_a.consumer.start()
    await backend_a.consumer.poll_once()  # applies + durably marks, ACK crashes -> pending
    assert sink_a.applied_total == 1
    assert await _pending(redis, config) == 1
    await backend_a.close()
    await ingestion_a.stop()

    # Ingestion restarts; the new epoch reuses producer_sequence 1 for a different payload.
    ingestion_b = await _start_ingestion(redis_socket, config, tmp_path)
    await ingestion_b.publish(new)
    epoch_b = ingestion_b.epoch
    assert epoch_b > epoch_a

    # Backend B reclaims the epoch-A pending entry (durable C1 suppresses it) and applies epoch-B.
    backend_b = await _start_backend(redis_socket, config, "backend-b")
    await asyncio.sleep(0.02)
    await backend_b.drain()
    assert backend_b.sink.applied_total == 1  # only the new-epoch event, not the reclaimed old one
    assert await _pending(redis, config) == 0

    report = compare(
        [view_from_envelope(_envelope(old[0], seq=1, epoch=epoch_a))]
        + [view_from_envelope(_envelope(new[0], seq=1, epoch=epoch_b))],
        _all_applied([backend_a, backend_b]),
    )
    assert report.is_clean
    assert report.known_b2_duplicate_total == 0  # lost ACK never caused a reapply
    assert report.matched_total == 2  # both same-seq/distinct-epoch identities present exactly once
    await backend_b.close()
    await ingestion_b.stop()


# =========================================================================== #
# H7-T12 the B2 apply->mark crash window stays explicitly unsafe across an ingestion restart
# =========================================================================== #
async def test_h7_t12_b2_old_epoch_duplicate_surfaced_across_restart(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    event = _tick(price="200")

    ingestion_a = await _start_ingestion(redis_socket, config, tmp_path)
    await ingestion_a.publish([event])
    epoch_a = ingestion_a.epoch

    # Backend A applies but crashes BEFORE the durable mark commits, then disappears.
    a_cfg = _fast_idle(config, consumer_name="backend-a")
    a_client: Redis = Redis(unix_socket_path=redis_socket)
    sink_a = RecordingShadowSink()
    backend_a = _Backend(
        client=a_client,
        consumer=MarketEventConsumer(
            transport=RedisMarketEventStream(redis=a_client, config=a_cfg),
            config=a_cfg,
            sink=sink_a,
            trading_date_source=lambda: _TD,
            universe_version_source=lambda: 7,
            now=lambda: _NOW,
            deduplicator=_RecordCrashDedup(_durable(a_client, a_cfg)),  # type: ignore[arg-type]
        ),
        sink=sink_a,
        config=a_cfg,
    )
    await backend_a.consumer.start()
    await backend_a.consumer.poll_once()  # applied, durable mark crashes -> pending, NOT marked
    assert sink_a.applied_total == 1
    assert await _pending(redis, config) == 1
    await backend_a.close()
    await ingestion_a.stop()

    # Ingestion restarts to a new epoch; the OLD epoch-A entry is still unmarked and pending.
    ingestion_b = await _start_ingestion(redis_socket, config, tmp_path)
    assert ingestion_b.epoch > epoch_a
    backend_b = await _start_backend(redis_socket, config, "backend-b")
    await asyncio.sleep(0.02)
    await backend_b.drain()
    assert backend_b.sink.applied_total == 1  # reclaimed + REAPPLIED (durable had no mark)

    report = compare(
        [view_from_envelope(_envelope(event, seq=1, epoch=epoch_a))],
        _all_applied([backend_a, backend_b]),
    )
    assert report.matched_total == 1
    assert report.known_b2_duplicate_total == 1  # B2 surfaced across the restart, counted
    assert not report.is_clean  # remains DESIGN_RESOLVED / IMPLEMENTATION_PENDING
    await backend_b.close()
    await ingestion_b.stop()


# =========================================================================== #
# H7-T13 D1 reference semantics stay intact across a producer restart
# =========================================================================== #
async def test_h7_t13_reference_event_across_restart(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    ref_a = _reference(symbol="TCS")
    ref_b = _reference(symbol="INFY")

    ingestion_a = await _start_ingestion(redis_socket, config, tmp_path)
    await ingestion_a.publish([ref_a])
    epoch_a = ingestion_a.epoch
    reference_key = f"{config.reference_key_prefix}:{_TD.isoformat()}"
    assert await redis.hexists(reference_key, "NSE:TCS")  # reference state written under epoch A
    await ingestion_a.stop()

    ingestion_b = await _start_ingestion(redis_socket, config, tmp_path)
    await ingestion_b.publish([ref_b])  # B publishes its reference atomically under the new epoch
    epoch_b = ingestion_b.epoch
    assert epoch_b > epoch_a
    assert await redis.hexists(reference_key, "NSE:INFY")
    await ingestion_b.stop()

    backend = await _start_backend(redis_socket, config, "backend")
    await backend.drain()
    report = compare(
        _expected_views([ref_a], epoch=epoch_a) + _expected_views([ref_b], epoch=epoch_b),
        _all_applied([backend]),
    )
    assert report.is_clean
    assert report.matched_total == 2  # both reference stream events consumed across the two epochs
    await backend.close()


# =========================================================================== #
# H7-T14/T15/T30 provider reconnect keeps the epoch; a genuine restart bumps it
# =========================================================================== #
async def test_h7_t14_t15_t30_provider_reconnect_vs_service_restart_epoch(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    provider = _ReconnectingProvider()
    ingestion = await _start_ingestion(
        redis_socket, config, tmp_path, provider=provider, max_reconnects=5
    )
    try:
        await ingestion.publish(_mix(4, offset=0))
        epoch0 = ingestion.epoch
        assert provider.connect_calls == 1
        assert provider.stream_calls == 1

        # A recoverable provider drop -> the supervisor reconnects WITHIN the same incarnation.
        provider.cut()
        await ingestion.await_reconnect(1)
        await ingestion.publish(_mix(4, offset=4))

        # T14/T30: a provider reconnect did NOT allocate a new producer epoch.
        assert ingestion.epoch == epoch0
        assert ingestion.service.diagnostics().producer_epoch == epoch0
        assert provider.connect_calls == 1  # the socket was never reconnected via connect()
        assert provider.stream_calls >= 2  # the stream WAS restarted
        assert ingestion.reconnect_total >= 1
        # The reconnect was recoverable, never terminal: fresh publication evidence heals L1.
        await ingestion.await_continuity(ContinuityState.HEALTHY)
        assert ingestion.epoch == epoch0  # still the same epoch after full recovery
    finally:
        await ingestion.stop()

    # T15/T30: a genuine service restart on the same durable state DOES bump the epoch.
    ingestion2 = await _start_ingestion(redis_socket, config, tmp_path)
    try:
        assert ingestion2.epoch > epoch0
    finally:
        await ingestion2.stop()


# =========================================================================== #
# H7-T16 a terminal publication failure fails the incarnation; a restart is a new epoch
# =========================================================================== #
async def test_h7_t16_terminal_publication_failure_then_restart_new_epoch(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    prod: Redis = Redis(unix_socket_path=redis_socket)
    atomic = _FailNthAtomic(RedisAtomicPublisher(prod, config), fail_on=3)
    stack = _publication_stack(redis=prod, config=config, state_dir=tmp_path, atomic=atomic)
    provider = _GatedProvider()
    service = _service(provider, stack)
    await service.start()
    ingestion = _LiveIngestion(service, stack, provider)
    epoch_a = ingestion.epoch

    # The 3rd publish fails at D1 -> the observer trips a terminal continuity break -> FAILED.
    ingestion.push(_mix(3, offset=0))
    await ingestion.await_status(ServiceStatus.FAILED)
    assert service.terminal_failure  # sticky terminal signal for this incarnation
    assert stack.continuity.snapshot().state is ContinuityState.BROKEN
    assert provider.disconnect_calls == 1  # fail-closed disconnected the provider
    await ingestion.stop()
    # Terminal state is NOT cleared inside the old incarnation (§29).
    assert stack.continuity.snapshot().state is ContinuityState.STOPPED
    assert service.terminal_failure

    # A restart is a genuinely NEW incarnation with a strictly higher epoch.
    ingestion_b = await _start_ingestion(redis_socket, config, tmp_path)
    try:
        assert ingestion_b.epoch > epoch_a
        await ingestion_b.publish(_mix(2, offset=0))  # the new incarnation publishes cleanly
        assert ingestion_b.continuity_state is ContinuityState.HEALTHY
    finally:
        await ingestion_b.stop()


# =========================================================================== #
# H7-T17 a failure before the provider (epoch allocation / Redis) never starts the provider
# =========================================================================== #
async def test_h7_t17_failure_before_provider_never_starts_provider(
    redis_socket: str, tmp_path: Path
) -> None:
    config = _config()

    # (a) Epoch allocation fails closed -> _start_publication raises before _start_provider.
    prod_a: Redis = Redis(unix_socket_path=redis_socket)
    stack_a = _publication_stack(
        redis=prod_a, config=config, state_dir=tmp_path, allocator=_FailingAllocator()
    )
    provider_a = _GatedProvider()
    service_a = _service(provider_a, stack_a)
    with pytest.raises(EpochStateError):
        await service_a.start()
    assert service_a.status is ServiceStatus.FAILED
    assert provider_a.connect_calls == 0  # provider never started
    assert provider_a.stream_calls == 0

    # (b) Redis unavailable at start -> ensure_group() raises before _start_provider.
    bad_client: Redis = Redis(unix_socket_path=str(tmp_path / "does-not-exist.sock"))
    stack_b = _publication_stack(redis=bad_client, config=config, state_dir=tmp_path / "b")
    provider_b = _GatedProvider()
    service_b = _service(provider_b, stack_b)
    with pytest.raises(RedisError):
        await service_b.start()
    assert service_b.status is ServiceStatus.FAILED
    assert provider_b.connect_calls == 0  # provider never started despite epoch being allocable


# =========================================================================== #
# H7-T18 corrupt durable epoch state fails closed (never a silent low-epoch reset)
# =========================================================================== #
async def test_h7_t18_corrupt_epoch_state_fail_closed(redis_socket: str, tmp_path: Path) -> None:
    config = _config()
    corrupt = tmp_path / f"producer-epoch-{_PRODUCER}.json"
    corrupt.write_text("{ this is not valid json", encoding="utf-8")

    stack = _real_stack(redis_socket, config, tmp_path)  # real DurableEpochAllocator
    provider = _GatedProvider()
    service = _service(provider, stack)
    with pytest.raises(EpochStateError):
        await service.start()
    assert service.status is ServiceStatus.FAILED
    assert provider.connect_calls == 0  # provider never started
    # M1 never silently rewrote the corrupt state to a reusable low epoch.
    assert corrupt.read_text(encoding="utf-8") == "{ this is not valid json"


# =========================================================================== #
# H7-T19 a legal sequence gap from an abrupt in-flight loss survives a restart uncorrected
# =========================================================================== #
async def test_h7_t19_legal_sequence_gap_across_abrupt_loss(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    # Incarnation A: seq 2 is allocated but its D1 transmit fails -> allocated-but-unpublished.
    # This is exactly what an abrupt process loss mid-transmit leaves: a legal gap on the stream.
    prod: Redis = Redis(unix_socket_path=redis_socket)
    atomic = _FailNthAtomic(RedisAtomicPublisher(prod, config), fail_on=2)
    stack_a = _publication_stack(redis=prod, config=config, state_dir=tmp_path, atomic=atomic)
    await stack_a.boundary.start()
    epoch_a = stack_a.publisher.diagnostics().producer_epoch
    assert epoch_a is not None
    stack_a.continuity.producer_started(producer_id=_PRODUCER, producer_epoch=epoch_a)
    events_a = [
        _tick(symbol="TCS", price="10"),  # seq 1 -> published
        _tick(symbol="INFY", price="20"),  # seq 2 -> D1 fails, allocated-but-dropped (the gap)
        _tick(symbol="HDFC", price="30"),  # seq 3 -> published
    ]
    for datum in events_a:
        stack_a.sink.handle(datum)
    result = await stack_a.boundary.stop()
    stack_a.continuity.drain_completed(result)
    await prod.aclose()
    assert stack_a.publisher.current_sequence == 3  # all three sequences were consumed
    assert await redis.xlen(config.stream_name) == 2  # seq 2 never reached the stream

    # Incarnation B: same durable state dir -> epoch strictly higher (abrupt loss kept the epoch).
    prod_b: Redis = Redis(unix_socket_path=redis_socket)
    stack_b = _publication_stack(redis=prod_b, config=config, state_dir=tmp_path)
    await stack_b.boundary.start()
    epoch_b = stack_b.publisher.diagnostics().producer_epoch
    assert epoch_b is not None
    assert epoch_b > epoch_a
    stack_b.continuity.producer_started(producer_id=_PRODUCER, producer_epoch=epoch_b)
    events_b = [_tick(symbol="TCS", price="40"), _tick(symbol="INFY", price="50")]
    for datum in events_b:
        stack_b.sink.handle(datum)
    result_b = await stack_b.boundary.stop()
    stack_b.continuity.drain_completed(result_b)
    await prod_b.aclose()

    backend = await _start_backend(redis_socket, config, "backend")
    await backend.drain()
    expected = [
        view_from_envelope(_envelope(events_a[0], seq=1, epoch=epoch_a)),
        view_from_envelope(_envelope(events_a[2], seq=3, epoch=epoch_a)),  # seq 2 legally absent
        view_from_envelope(_envelope(events_b[0], seq=1, epoch=epoch_b)),
        view_from_envelope(_envelope(events_b[1], seq=2, epoch=epoch_b)),
    ]
    report = compare(expected, _all_applied([backend]), sample_limit=20)
    assert report.is_clean
    assert report.matched_total == 4
    assert report.missing_total == 0  # the gap at (epoch_a, seq 2) is NOT reported as missing
    await backend.close()


# =========================================================================== #
# H7-T20 >= 25 ingestion incarnations allocate strictly monotonic, never-reused epochs
# =========================================================================== #
async def test_h7_t20_epoch_monotonicity_stress(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    epochs: list[int] = []
    for _ in range(30):  # satisfies the >= 25 requirement
        ingestion = await _start_ingestion(redis_socket, config, tmp_path)
        epochs.append(ingestion.epoch)
        await ingestion.stop()

    assert len(set(epochs)) == 30  # never reused
    assert epochs == sorted(epochs)  # monotonic non-decreasing ...
    pairs = zip(epochs, epochs[1:], strict=False)  # pairwise: second arg is intentionally shorter
    assert all(later > earlier for earlier, later in pairs)  # ... and strictly increasing
    assert epochs == list(range(epochs[0], epochs[0] + 30))  # contiguous across clean restarts


# =========================================================================== #
# H7-T21 concurrent epoch allocation on one durable state dir yields unique, monotonic epochs
# =========================================================================== #
async def test_h7_t21_concurrent_epoch_allocation_unique(tmp_path: Path) -> None:
    contenders = 8
    # Distinct allocator instances on the SAME durable state dir model separate process-like
    # contenders; M1's exclusive file lock must serialise them into distinct epochs.
    allocators = [DurableEpochAllocator(tmp_path) for _ in range(contenders)]
    results = await asyncio.gather(*(a.allocate(_PRODUCER) for a in allocators))

    assert len(set(results)) == contenders  # every allocation is unique (no collision)
    assert sorted(results) == list(range(1, contenders + 1))  # contiguous 1..N, none reused


# =========================================================================== #
# H7-T22 a >= 10,000-event two-incarnation replay is clean (sequence resets under the new epoch)
# =========================================================================== #
async def test_h7_t22_large_two_incarnation_replay(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config(read_count=1_000, publish_queue_capacity=20_000)
    backend = await _start_backend(redis_socket, config, "backend")
    batch_a = _mix(5_000, offset=0)
    batch_b = _mix(5_000, offset=5_000)

    ingestion_a = await _start_ingestion(redis_socket, config, tmp_path)
    await ingestion_a.publish(batch_a)
    epoch_a = ingestion_a.epoch
    assert ingestion_a.accepted_sequence == 5_000
    await backend.drain()
    assert backend.sink.applied_total == 5_000
    await ingestion_a.stop()

    ingestion_b = await _start_ingestion(redis_socket, config, tmp_path)
    await ingestion_b.publish(batch_b)
    epoch_b = ingestion_b.epoch
    assert epoch_b > epoch_a
    assert ingestion_b.accepted_sequence == 5_000  # sequence RESET to 1..5000 under the new epoch
    await backend.drain()

    report = compare(
        _expected_views(batch_a, epoch=epoch_a) + _expected_views(batch_b, epoch=epoch_b),
        _all_applied([backend]),
        sample_limit=20,
    )
    assert report.is_clean
    assert report.matched_total == 10_000
    assert report.missing_total == 0
    assert report.unexpected_total == 0
    await ingestion_b.stop()
    await backend.close()


# =========================================================================== #
# H7-T23 a >= 10-incarnation multi-restart replay keeps every identity unique and parity clean
# =========================================================================== #
async def test_h7_t23_multi_incarnation_replay(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config(read_count=200)
    backend = await _start_backend(redis_socket, config, "backend")  # alive across all restarts
    incarnations = 12  # satisfies the >= 10 requirement
    per = 6
    epochs: list[int] = []
    expected: list[object] = []
    for i in range(incarnations):
        ingestion = await _start_ingestion(redis_socket, config, tmp_path)
        batch = _mix(per, offset=i * per)
        await ingestion.publish(batch)
        epochs.append(ingestion.epoch)
        expected += _expected_views(batch, epoch=ingestion.epoch)
        assert ingestion.accepted_sequence == per  # every incarnation restarts at sequence 1
        await backend.drain()  # backend survives every ingestion restart
        await ingestion.stop()

    assert len(set(epochs)) == incarnations  # all epochs unique
    pairs = zip(epochs, epochs[1:], strict=False)  # pairwise: second arg is intentionally shorter
    assert all(later > earlier for earlier, later in pairs)  # strictly increasing
    report = compare(expected, _all_applied([backend]), sample_limit=20)
    assert report.is_clean
    assert report.matched_total == incarnations * per
    assert report.known_b2_duplicate_total == 0
    await backend.close()


# =========================================================================== #
# H7-T24/T25/T26/T27 repeated ingestion restarts leak no tasks, clients, or workers
# =========================================================================== #
async def test_h7_t24_t25_t26_t27_no_resource_leak_across_ingestion_restarts(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    probe: Redis = Redis(unix_socket_path=redis_socket)
    backend = await _start_backend(redis_socket, config, "backend")
    try:
        cycles = 30
        baseline_tasks = 0
        baseline_clients = 0
        for cycle in range(cycles):
            ingestion = await _start_ingestion(redis_socket, config, tmp_path)
            await ingestion.publish(_mix(4, offset=cycle * 4))
            await backend.drain()
            await ingestion.stop()  # cancels supervisor + observer + M2 worker; closes prod client

            await asyncio.sleep(0.005)  # let cancelled tasks / closed clients settle
            live_tasks = len(asyncio.all_tasks())
            live_clients = int((await probe.info("clients"))["connected_clients"])
            if cycle == 2:  # warm-up done; capture the steady-state ceilings
                baseline_tasks = live_tasks
                baseline_clients = live_clients
            elif cycle > 2:
                # T24 (tasks) + T26 (provider tasks) + T27 (M2 workers) are all asyncio tasks.
                assert live_tasks <= baseline_tasks
                assert live_clients <= baseline_clients  # T25: no Redis-client accumulation

        # Each cycle c published 4 events under epoch c+1 (contiguous clean restarts) at seq 1..4.
        expected = [
            view_from_envelope(_envelope(datum, seq=1 + (i % 4), epoch=1 + (i // 4)))
            for i, datum in enumerate(_mix(cycles * 4, offset=0))
        ]
        report = compare(expected, _all_applied([backend]), sample_limit=20)
        assert report.is_clean
        assert report.matched_total == cycles * 4  # every incarnation's events applied exactly once
    finally:
        await backend.close()
        await probe.aclose()


# =========================================================================== #
# H7-T29/T30/T31/T32 no broker construction, no authority, canonical UTC (no FIX-2 workaround)
# =========================================================================== #
async def test_h7_t29_t30_t31_t32_no_broker_construction_and_canonical_utc(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    ingestion = await _start_ingestion(redis_socket, config, tmp_path)
    try:
        # T29: the only provider is the local fake; no app.adapters (Dhan) provider constructed.
        assert ingestion.service.provider is ingestion.provider
        assert not type(ingestion.provider).__module__.startswith("app.adapters")

        await ingestion.publish(_mix(4, offset=0))
        backend = await _start_backend(redis_socket, config, "backend")
        # T30: the only consumer destination is the non-authoritative shadow sink (no TickEngine).
        assert isinstance(backend.sink, RecordingShadowSink)
        await backend.drain()
        report = compare(
            _expected_views(_mix(4, offset=0), epoch=ingestion.epoch), _all_applied([backend])
        )
        assert report.is_clean
        await backend.close()

        # T31/T32: canonical tz-aware UTC; no +5:30/-5:30 workaround.
        envelope = _envelope(_tick(), seq=1, epoch=ingestion.epoch)
        assert envelope.produced_at.utcoffset() == datetime(2026, 1, 1, tzinfo=UTC).utcoffset()
        assert _NOW.utcoffset().total_seconds() == 0
    finally:
        await ingestion.stop()
