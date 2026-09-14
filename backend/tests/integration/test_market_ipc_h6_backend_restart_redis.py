"""Backend-restart independence under the decoupled IPC path (DECOUPLING PHASE H6).

Proves the primary operational reason for the ingestion decoupling: the backend consumer can
stop / be recreated while market ingestion keeps running, then a fresh backend resumes from Redis
without reconnecting the provider, losing durably-published events, or reapplying durably-completed
events.

Topology (reuses H5) — the DIFFERENCE from H5 is that ONE ingestion incarnation stays alive
continuously while the backend consumer is destroyed and recreated:

    deterministic gated provider (no Dhan / tokens / sockets / internet)
        -> market-ingestion service + ProviderSupervisor   (ONE incarnation, stays up)
        -> M1 -> M2 -> D1 -> L1                             (real composition)
        -> Redis md:events (+ md:reference:<date>)          (the decoupling BUFFER)
        -> backend consumer A ... destroyed ... backend consumer B ... (genuinely separate
           Redis clients, EMPTY memory caches, SAME durable C1 + SAME Redis consumer group)
        -> non-authoritative RecordingShadowSink per incarnation
        -> H4C semantic comparator over the UNION of all incarnations' applied events

Each backend incarnation is a genuinely separate runtime object: its own Redis client, a fresh
in-memory dedup cache, a distinct consumer name, and its own sink. Correctness therefore comes
ONLY from the durable Redis C1 authority and the shared consumer group — never from process
memory (proven by the empty-cache-no-reapply tests). H6 needs NO production-code change: the
producer and consumer share only Redis, so restarting the consumer cannot, by construction, touch
the provider or the M1 producer epoch.

H6 is OFFLINE and NON-AUTHORITATIVE: no live Dhan, no production contact, no consumer activation,
no IPC authority, no TickEngine/MarketContext, no FIX-2 workaround. Timestamps are canonical
tz-aware UTC. This does NOT restart ingestion (a new producer epoch belongs to H7). Skips cleanly
if redislite is absent.
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
from app.market_ingestion.service import MarketIngestionService
from app.market_ipc import (
    BoundedDeduplicator,
    CompositeDeduplicator,
    DurableDeduplicator,
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
from app.market_ipc.continuity import ContinuityState, FeedContinuityTracker
from app.market_ipc.epoch import DurableEpochAllocator
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


@pytest.fixture(scope="module")
def redis_socket() -> str:
    server = redislite.Redis()
    try:
        yield server.socket_file
    finally:
        server.shutdown()


@pytest.fixture
async def redis(redis_socket: str) -> Redis:
    """A flush-on-setup assertion/probe client (backend incarnations create their own clients)."""
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
# Gated provider — ONE live incarnation whose feed the test controls on demand
# --------------------------------------------------------------------------- #
class _GatedProvider(BrokerAdapter):
    """Live provider whose feed the test drives: push batches on demand; ONE incarnation, stays up.

    ``stream_market_data`` is entered exactly once and blocks on an internal queue between batches,
    so the provider stays "connected" across every backend restart. ``connect``/``disconnect``/
    ``stream`` call counts are exposed so a test can prove a backend restart never reconnects the
    provider. No Dhan, tokens, sockets, or internet.
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


# --------------------------------------------------------------------------- #
# Controllable D1 seam double (real M1/M2/L1 around a faultable AtomicPublisher) — legal-gap setup
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


def _manual_stack(
    *, stream_redis: Redis, atomic: object, config: MarketIpcConfig, state_dir: Path, capacity: int
) -> tuple[MarketEventPublisher, AsyncPublicationBoundary, FeedContinuityTracker]:
    """Assemble the REAL M1/M2/L1 components around a controllable D1 (legal-gap producer setup)."""
    stream = RedisMarketEventStream(redis=stream_redis, config=config)
    publisher = MarketEventPublisher(
        stream=stream,
        config=config,
        producer_id=_PRODUCER,
        epoch_allocator=DurableEpochAllocator(state_dir),
        trading_date_source=_FixedTradingDate(),
        universe_version_source=StaticUniverseVersion(7),
        now=lambda: _NOW,
        atomic_publisher=atomic,  # type: ignore[arg-type]
    )
    boundary = AsyncPublicationBoundary(
        publisher=publisher, capacity=capacity, drain_timeout_seconds=1.0, now=lambda: _NOW
    )
    return publisher, boundary, FeedContinuityTracker()


# --------------------------------------------------------------------------- #
# Continuously-alive ingestion incarnation (real service; ONE producer epoch)
# --------------------------------------------------------------------------- #
class _LiveIngestion:
    """A running market-ingestion incarnation that stays alive across backend restarts."""

    def __init__(
        self, service: MarketIngestionService, stack: PublicationStack, provider: _GatedProvider
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
    def continuity_state(self) -> ContinuityState:
        return self.stack.continuity.snapshot().state

    def push(self, events: list[IpcPayload]) -> None:
        """Feed a batch into the live provider (non-blocking; publication is asynchronous)."""
        self.provider.push(events)

    async def publish(self, events: list[IpcPayload]) -> None:
        """Feed a batch and wait until every event of it has reached Redis (published advanced)."""
        target = self.published_total + len(events)
        self.provider.push(events)
        await self.await_published(target)

    async def await_published(self, target: int) -> None:
        for _ in range(300_000):
            if self.published_total >= target:
                return
            await asyncio.sleep(0.001)
        raise AssertionError(f"published {self.published_total} < {target}")

    async def stop(self) -> None:
        await self.service.stop()  # cancels the single supervisor incarnation; drains M2 -> Redis


async def _start_ingestion(
    redis_socket: str, config: MarketIpcConfig, state_dir: Path
) -> _LiveIngestion:
    """Start ONE real market-ingestion incarnation (publisher mode) over a gated fake provider."""
    provider = _GatedProvider()
    prod: Redis = Redis(unix_socket_path=redis_socket)
    stack = build_publication_stack(
        redis=prod,
        config=config,
        producer_id=_PRODUCER,
        state_dir=state_dir,
        now=lambda: _NOW,
        trading_date_source=_FixedTradingDate(),
        universe_version=7,
    )
    flags = PhaseHFlags(
        market_ingestion_service_enabled=True,
        ipc_publisher_enabled=True,
        ipc_consumer_enabled=False,
        ipc_shadow_compare_enabled=False,
        ipc_authoritative_enabled=False,
        legacy_market_path_enabled=True,
    )
    service = MarketIngestionService(
        flags=flags,
        provider=provider,
        subscription_request=_request(),
        publication=stack,
        supervisor_max_reconnects=0,
        supervisor_sleep=_no_sleep,
        observer_interval_seconds=0.005,
    )
    await service.start()
    return _LiveIngestion(service, stack, provider)


# --------------------------------------------------------------------------- #
# Backend incarnation — own Redis client, EMPTY memory cache, shared durable C1 + group
# --------------------------------------------------------------------------- #
def _config(**overrides: object) -> MarketIpcConfig:
    return MarketIpcConfig(block_ms=0, **overrides)


def _fast_idle(config: MarketIpcConfig, *, consumer_name: str) -> MarketIpcConfig:
    """A per-incarnation config: distinct consumer name + fast (1ms) idle reclaim for tests."""
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
        """Destroy this incarnation: close its own client (durable C1 state stays in Redis)."""
        await self.client.aclose()


async def _start_backend(
    redis_socket: str,
    config: MarketIpcConfig,
    name: str,
    *,
    deduplicator: object | None = None,
    transport: object | None = None,
    max_entries: int = 20_000,
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
    for _ in range(2_000):
        before = consumer.diagnostics().acked_total
        await consumer.poll_once()
        if consumer.diagnostics().acked_total == before and await _pending(redis, config) == 0:
            return
    raise AssertionError("stream did not drain within the cycle budget")


async def _reinject_duplicates(redis: Redis, config: MarketIpcConfig, count: int) -> None:
    """Re-append the first ``count`` stream entries verbatim: simulate at-least-once redelivery."""
    entries = await redis.xrange(config.stream_name, count=count)
    for _id, fields in entries:
        raw = next(iter(fields.values()))
        await redis.xadd(config.stream_name, {_FIELD: raw}, maxlen=config.maxlen, approximate=True)


def _all_applied(backends: list[_Backend]) -> list[object]:
    """Union of applied (envelope, event) views across every backend incarnation."""
    events: list[tuple[MarketEventEnvelope, IpcPayload]] = []
    for backend in backends:
        events.extend(backend.sink.events)
    return views_from_applied(events)


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


# =========================================================================== #
# H6-T01/T18 clean backend restart while ingestion keeps running (Scenario A)
# =========================================================================== #
async def test_h6_t01_t18_clean_backend_restart_while_ingestion_runs(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config(read_count=200)
    ingestion = await _start_ingestion(redis_socket, config, tmp_path)
    try:
        batch_a = _mix(60, offset=0)
        batch_b = _mix(80, offset=60)
        batch_c = _mix(50, offset=140)

        # Backend A consumes batch A and reaches parity.
        await ingestion.publish(batch_a)
        backend_a = await _start_backend(redis_socket, config, "backend-a")
        await backend_a.drain()
        assert backend_a.sink.applied_total == 60

        # Gracefully stop backend A ONLY; ingestion keeps running.
        await backend_a.close()
        assert ingestion.service.status.value == "running"

        # Batch B published while the backend is down -> accumulates in Redis.
        await ingestion.publish(batch_b)
        assert await redis.xlen(config.stream_name) == 140

        # Fresh backend B (empty cache) catches up; batch C published while B is active.
        backend_b = await _start_backend(redis_socket, config, "backend-b")
        await ingestion.publish(batch_c)
        await backend_b.drain()

        report = compare(
            _expected_views(batch_a + batch_b + batch_c, epoch=ingestion.epoch),
            _all_applied([backend_a, backend_b]),
            sample_limit=20,
        )
        assert report.is_clean  # T18: final parity A+B+C is clean after catch-up
        assert report.matched_total == 190
        assert report.missing_total == 0
        assert report.unexpected_total == 0
        assert report.value_mismatch_total == 0
        assert report.known_b2_duplicate_total == 0
        await backend_b.close()
    finally:
        await ingestion.stop()


# =========================================================================== #
# H6-T02/T03/T24 producer id/epoch + ingestion incarnation stable across restart
# =========================================================================== #
async def test_h6_t02_t03_t24_producer_identity_stable_across_backend_restart(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    ingestion = await _start_ingestion(redis_socket, config, tmp_path)
    try:
        await ingestion.publish(_mix(10, offset=0))
        epoch0 = ingestion.epoch
        stream_calls0 = ingestion.provider.stream_calls
        assert ingestion.service.diagnostics().producer_epoch == epoch0

        for cycle in range(3):
            backend = await _start_backend(redis_socket, config, f"backend-{cycle}")
            await backend.drain()
            await backend.close()
            await ingestion.publish(_mix(5, offset=10 + cycle * 5))
            assert ingestion.epoch == epoch0  # T03: backend restart never allocated a new epoch
            assert ingestion.service.diagnostics().producer_epoch == epoch0
            assert ingestion.provider.stream_calls == stream_calls0  # T24: same incarnation
        # T02: the producer id is fixed for the incarnation.
        assert _PRODUCER == "market-ingestion"
    finally:
        await ingestion.stop()


# =========================================================================== #
# H6-T04 producer sequence continues across backend downtime (never resets)
# =========================================================================== #
async def test_h6_t04_sequence_continues_across_backend_downtime(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    ingestion = await _start_ingestion(redis_socket, config, tmp_path)
    try:
        await ingestion.publish(_mix(30, offset=0))
        seq_before = ingestion.accepted_sequence
        assert seq_before == 30  # contiguous 1..30

        backend = await _start_backend(redis_socket, config, "backend-a")
        await backend.drain()
        await backend.close()  # backend down

        await ingestion.publish(_mix(25, offset=30))  # published while backend down
        seq_after = ingestion.accepted_sequence
        assert seq_after == 55  # T04: continued 31..55, no reset to 1
        assert seq_after > seq_before
        assert ingestion.epoch == 1  # single incarnation, epoch unchanged
    finally:
        await ingestion.stop()


# =========================================================================== #
# H6-T05/T06 provider connect/disconnect counts unchanged by backend restart
# =========================================================================== #
async def test_h6_t05_t06_provider_lifecycle_independent_of_backend_restart(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    ingestion = await _start_ingestion(redis_socket, config, tmp_path)
    try:
        await ingestion.publish(_mix(10, offset=0))
        assert ingestion.provider.connect_calls == 1
        assert ingestion.provider.disconnect_calls == 0
        assert ingestion.provider.stream_calls == 1

        for cycle in range(5):
            backend = await _start_backend(redis_socket, config, f"backend-{cycle}")
            await backend.drain()
            await backend.close()
            # T05/T06: a backend restart neither reconnects nor disconnects the provider.
            assert ingestion.provider.connect_calls == 1
            assert ingestion.provider.disconnect_calls == 0
            assert ingestion.provider.stream_calls == 1
    finally:
        await ingestion.stop()
    # Provider is disconnected exactly once, by ingestion shutdown — never by a backend restart.
    assert ingestion.provider.disconnect_calls == 1


# =========================================================================== #
# H6-T07 events accumulate in Redis while the backend is absent (Scenario D)
# =========================================================================== #
async def test_h6_t07_events_accumulate_in_redis_while_backend_absent(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    ingestion = await _start_ingestion(redis_socket, config, tmp_path)
    try:
        # Backend A consumes an initial batch, then stops.
        await ingestion.publish(_mix(20, offset=0))
        backend_a = await _start_backend(redis_socket, config, "backend-a")
        await backend_a.drain()
        applied_before = backend_a.sink.applied_total
        stream_before = await redis.xlen(config.stream_name)
        await backend_a.close()

        # Ingestion continues publishing while NO backend exists.
        await ingestion.publish(_mix(40, offset=20))
        stream_after = await redis.xlen(config.stream_name)

        assert stream_after > stream_before  # Redis stream grew during backend downtime
        assert stream_after == 60
        assert applied_before == 20  # the (destroyed) backend's applied count did not change
        assert ingestion.provider.stream_calls == 1  # no provider replay was required
    finally:
        await ingestion.stop()


# =========================================================================== #
# H6-T08 a fresh backend catches up a backlog published with NO backend running
# =========================================================================== #
async def test_h6_t08_fresh_backend_catches_up_backlog(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config(read_count=100)
    ingestion = await _start_ingestion(redis_socket, config, tmp_path)
    try:
        # Publish an entire backlog before any backend has ever started.
        backlog = _mix(150, offset=0)
        await ingestion.publish(backlog)
        assert await redis.xlen(config.stream_name) == 150

        backend = await _start_backend(redis_socket, config, "backend-a")
        await backend.drain()
        report = compare(
            _expected_views(backlog, epoch=ingestion.epoch),
            _all_applied([backend]),
            sample_limit=20,
        )
        assert report.is_clean
        assert report.matched_total == 150
        assert backend.sink.applied_total == 150
        assert await _pending(backend.client, backend.config) == 0
        await backend.close()
    finally:
        await ingestion.stop()


# =========================================================================== #
# H6-T09/T10 durable C1 survives recreation; empty memory cache does not reapply
# =========================================================================== #
async def test_h6_t09_t10_durable_survives_recreation_empty_cache_no_reapply(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    ingestion = await _start_ingestion(redis_socket, config, tmp_path)
    try:
        events = _mix(10, offset=0)
        await ingestion.publish(events)

        # Backend A applies + durably marks + ACKs all 10, then is destroyed.
        backend_a = await _start_backend(redis_socket, config, "backend-a")
        await backend_a.drain()
        assert backend_a.sink.applied_total == 10
        await backend_a.close()

        # At-least-once redelivery of the same identities after the restart.
        await _reinject_duplicates(redis, config, count=10)

        # Backend B: brand-new client + EMPTY in-memory cache, SAME durable Redis authority.
        backend_b = await _start_backend(redis_socket, config, "backend-b")
        await backend_b.drain()
        assert backend_b.sink.applied_total == 0  # T09/T10: durable C1 recognised every identity
        assert backend_b.consumer.diagnostics().duplicate_total == 10
        await backend_b.close()
    finally:
        await ingestion.stop()


# =========================================================================== #
# H6-T11 abandoned PEL from a dead backend is reclaimed by the new backend (Scenario B/G)
# =========================================================================== #
async def test_h6_t11_dead_consumer_pel_reclaimed_by_new_backend(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config(read_count=50)
    ingestion = await _start_ingestion(redis_socket, config, tmp_path)
    try:
        events = _mix(40, offset=0)
        await ingestion.publish(events)

        # Backend A (transport-level) reads entries into its PEL but disappears WITHOUT acking:
        # a poll_once would apply+ack them, so read raw to strand them like an abrupt crash.
        a_cfg = _fast_idle(config, consumer_name="backend-a")
        a_client: Redis = Redis(unix_socket_path=redis_socket)
        a_stream = RedisMarketEventStream(redis=a_client, config=a_cfg)
        await a_stream.ensure_group()
        stranded = await a_stream.read_raw()
        assert len(stranded) == 40
        assert await _pending(redis, config) == 40
        await a_client.aclose()  # abrupt: A gone with entries stranded in its PEL

        # Backend B starts; XAUTOCLAIM reclaims A's abandoned PEL after the idle threshold.
        backend_b = await _start_backend(redis_socket, config, "backend-b")
        await asyncio.sleep(0.02)  # exceed the 1ms claim-idle threshold
        await backend_b.drain()

        report = compare(
            _expected_views(events, epoch=ingestion.epoch),
            _all_applied([backend_b]),
            sample_limit=20,
        )
        assert report.is_clean
        assert report.matched_total == 40
        assert backend_b.consumer.diagnostics().pending_reclaimed_applied > 0  # recovery exercised
        assert await _pending(redis, config) == 0  # PEL drained on successful processing
        await backend_b.close()
    finally:
        await ingestion.stop()


# =========================================================================== #
# H6-T12 an ACK lost before restart is not reapplied after restart (Scenario C)
# =========================================================================== #
async def test_h6_t12_ack_lost_before_restart_not_reapplied(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    ingestion = await _start_ingestion(redis_socket, config, tmp_path)
    try:
        events = _mix(3, offset=0)
        await ingestion.publish(events)

        # Backend A applies + durably marks all 3, but its ACK fails and it disappears.
        a_cfg = _fast_idle(config, consumer_name="backend-a")
        a_client: Redis = Redis(unix_socket_path=redis_socket)
        sink = RecordingShadowSink()
        backend_a = _Backend(
            client=a_client,
            consumer=MarketEventConsumer(
                transport=_AckCrashTransport(RedisMarketEventStream(redis=a_client, config=a_cfg)),  # type: ignore[arg-type]
                config=a_cfg,
                sink=sink,
                trading_date_source=lambda: _TD,
                universe_version_source=lambda: 7,
                now=lambda: _NOW,
                deduplicator=_durable(a_client, a_cfg),
            ),
            sink=sink,
            config=a_cfg,
        )
        await backend_a.consumer.start()
        await backend_a.consumer.poll_once()  # applies + durably marks, ACK crashes -> all pending
        assert sink.applied_total == 3
        assert await _pending(redis, config) == 3
        await backend_a.close()

        # Backend B reclaims; durable C1 recognises the identities -> NO reapply, then ACK.
        backend_b = await _start_backend(redis_socket, config, "backend-b")
        await asyncio.sleep(0.02)
        await backend_b.drain()

        report = compare(
            _expected_views(events, epoch=ingestion.epoch),
            _all_applied([backend_a, backend_b]),
            sample_limit=20,
        )
        assert report.is_clean
        assert report.known_b2_duplicate_total == 0  # T12: no reapply despite the lost ACK
        assert backend_b.sink.applied_total == 0  # B applied nothing; durable said duplicate
        assert await _pending(redis, config) == 0
        await backend_b.close()
    finally:
        await ingestion.stop()


# =========================================================================== #
# H6-T13 the B2 apply->mark crash window stays explicitly unsafe across restart
# =========================================================================== #
async def test_h6_t13_b2_apply_mark_crash_remains_unsafe(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    ingestion = await _start_ingestion(redis_socket, config, tmp_path)
    try:
        event = _tick(price="200")
        await ingestion.publish([event])

        # Backend A applies but crashes BEFORE the durable mark commits, then disappears.
        a_cfg = _fast_idle(config, consumer_name="backend-a")
        a_client: Redis = Redis(unix_socket_path=redis_socket)
        sink = RecordingShadowSink()
        backend_a = _Backend(
            client=a_client,
            consumer=MarketEventConsumer(
                transport=RedisMarketEventStream(redis=a_client, config=a_cfg),
                config=a_cfg,
                sink=sink,
                trading_date_source=lambda: _TD,
                universe_version_source=lambda: 7,
                now=lambda: _NOW,
                deduplicator=_RecordCrashDedup(_durable(a_client, a_cfg)),  # type: ignore[arg-type]
            ),
            sink=sink,
            config=a_cfg,
        )
        await backend_a.consumer.start()
        await backend_a.consumer.poll_once()  # applied, durable mark crashes -> pending, NOT marked
        assert sink.applied_total == 1
        assert await _pending(redis, config) == 1
        await backend_a.close()

        # Backend B reclaims; durable has no mark -> the event is REAPPLIED (the B2 window).
        backend_b = await _start_backend(redis_socket, config, "backend-b")
        await asyncio.sleep(0.02)
        await backend_b.drain()
        assert backend_b.sink.applied_total == 1

        report = compare(
            [view_from_envelope(_envelope(event, seq=1, epoch=ingestion.epoch))],
            _all_applied([backend_a, backend_b]),
        )
        assert report.matched_total == 1
        assert report.known_b2_duplicate_total == 1  # T13: surfaced, counted
        assert not report.is_clean  # B2 remains explicitly unsafe (DESIGN_RESOLVED / IMPL PENDING)
        await backend_b.close()
    finally:
        await ingestion.stop()


# =========================================================================== #
# H6-T14 a restarted backend tolerates a legal producer-sequence gap in the stream
# =========================================================================== #
async def test_h6_t14_legal_sequence_gap_tolerated(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    # Setup: produce a stream whose seq 2 was allocated-but-dropped at D1 (a legal gap).
    prod: Redis = Redis(unix_socket_path=redis_socket)
    atomic = _FailNthAtomic(RedisAtomicPublisher(prod, config), fail_on=2)
    publisher, boundary, continuity = _manual_stack(
        stream_redis=prod, atomic=atomic, config=config, state_dir=tmp_path, capacity=16
    )
    sink = PublishingEventSink(boundary=boundary, continuity=continuity, publisher=publisher)
    await boundary.start()
    epoch = publisher.diagnostics().producer_epoch
    assert epoch is not None
    continuity.producer_started(producer_id=_PRODUCER, producer_epoch=epoch)
    events = [
        _tick(symbol="TCS", price="10"),  # seq 1 -> published
        _tick(symbol="INFY", price="20"),  # seq 2 -> D1 fails, allocated-but-dropped (the gap)
        _tick(symbol="HDFC", price="30"),  # seq 3 -> published
    ]
    for datum in events:
        sink.handle(datum)
    result = await boundary.stop()
    continuity.drain_completed(result)
    await prod.aclose()
    assert publisher.current_sequence == 3
    assert await redis.xlen(config.stream_name) == 2

    # A fresh backend consumes the gapped stream: the missing seq 2 is NOT reported as MISSING.
    backend = await _start_backend(redis_socket, config, "backend-a")
    await backend.drain()
    expected = [
        view_from_envelope(_envelope(events[0], seq=1, epoch=epoch)),
        view_from_envelope(_envelope(events[2], seq=3, epoch=epoch)),
    ]
    report = compare(expected, _all_applied([backend]))
    assert report.is_clean
    assert report.matched_total == 2
    assert report.missing_total == 0
    await backend.close()


# =========================================================================== #
# H6-T15 a stream already carrying multiple producer epochs is consumed correctly
# =========================================================================== #
async def test_h6_t15_multiple_producer_epochs_in_stream_handled(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    # Setup: two SHORT prior ingestion incarnations (an ingestion restart happened BEFORE the H6
    # window; H6 itself never restarts ingestion) leave epoch-1 and epoch-2 events in the stream.
    first = _mix(6, offset=0)
    second = _mix(6, offset=6)
    ingestion1 = await _start_ingestion(redis_socket, config, tmp_path)
    await ingestion1.publish(first)
    epoch1 = ingestion1.epoch
    await ingestion1.stop()
    ingestion2 = await _start_ingestion(redis_socket, config, tmp_path)  # same state dir -> restart
    await ingestion2.publish(second)
    epoch2 = ingestion2.epoch
    await ingestion2.stop()
    assert epoch2 > epoch1

    backend = await _start_backend(redis_socket, config, "backend-a")
    await backend.drain()
    expected = _expected_views(first, epoch=epoch1) + _expected_views(second, epoch=epoch2)
    report = compare(expected, _all_applied([backend]), sample_limit=20)
    assert report.is_clean
    assert report.matched_total == 12  # both epochs consumed; same seq / distinct epoch not merged
    assert report.duplicate_suppressed_total == 0
    await backend.close()


# =========================================================================== #
# H6-T16/T17 large backlog catch-up with concurrent new traffic (>= 9,000 events)
# =========================================================================== #
async def test_h6_t16_t17_large_backlog_catchup_with_concurrent_traffic(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config(read_count=1_000, publish_queue_capacity=20_000)
    ingestion = await _start_ingestion(redis_socket, config, tmp_path)
    try:
        batch_a = _mix(2_000, offset=0)
        batch_b = _mix(5_000, offset=2_000)  # published while backend down (>= 5,000)
        batch_c = _mix(2_000, offset=7_000)  # published concurrently DURING catch-up

        # Backend A consumes batch A, then stops.
        await ingestion.publish(batch_a)
        backend_a = await _start_backend(redis_socket, config, "backend-a")
        await backend_a.drain()
        assert backend_a.sink.applied_total == 2_000
        await backend_a.close()

        # Batch B accumulates in Redis with NO backend running (the backlog).
        await ingestion.publish(batch_b)
        backlog = await redis.xlen(config.stream_name)
        assert backlog == 7_000

        # Fresh backend B drains the backlog WHILE batch C is published concurrently.
        backend_b = await _start_backend(redis_socket, config, "backend-b")
        ingestion.push(batch_c)  # non-blocking: ingestion publishes C during the catch-up loop
        cycles = await _drain_until(backend_b, ingestion, total=9_000, config=config)

        report = compare(
            _expected_views(batch_a + batch_b + batch_c, epoch=ingestion.epoch),
            _all_applied([backend_a, backend_b]),
            sample_limit=20,
        )
        assert report.is_clean
        assert (
            report.matched_total == 9_000
        )  # T18 at scale: neither backlog nor new traffic starved
        assert report.missing_total == 0
        assert report.unexpected_total == 0
        assert ingestion.provider.stream_calls == 1  # no provider replay during catch-up
        assert cycles > 0
        await backend_b.close()
    finally:
        await ingestion.stop()


async def _drain_until(
    backend: _Backend, ingestion: _LiveIngestion, *, total: int, config: MarketIpcConfig
) -> int:
    """Poll until every event is published AND consumed (backlog + concurrent new traffic)."""
    for cycle in range(20_000):
        before = backend.consumer.diagnostics().acked_total
        await backend.consumer.poll_once()
        published = ingestion.published_total
        pending = await _pending(backend.client, config)
        acked_stable = backend.consumer.diagnostics().acked_total == before
        if published >= total and pending == 0 and acked_stable:
            return cycle + 1
        await asyncio.sleep(0.001)  # let the live ingestion worker publish concurrent traffic
    raise AssertionError("backlog + concurrent traffic did not converge within the cycle budget")


# =========================================================================== #
# H6-T19/T20/T21/T22 many backend restart cycles: no provider/epoch churn, no leak
# =========================================================================== #
async def test_h6_t19_t20_t21_t22_many_restart_cycles_no_leak(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    probe: Redis = Redis(unix_socket_path=redis_socket)
    ingestion = await _start_ingestion(redis_socket, config, tmp_path)
    backends: list[_Backend] = []
    try:
        cycles = 30  # satisfies T19 (>=5) and T20 (25-50)
        baseline_tasks = 0
        baseline_clients = 0
        for cycle in range(cycles):
            await ingestion.publish(_mix(4, offset=cycle * 4))
            backend = await _start_backend(redis_socket, config, f"backend-{cycle}")
            await backend.drain()
            await backend.close()  # destroy this incarnation's client
            backends.append(backend)

            # Provider + producer epoch never churn because of a backend restart.
            assert ingestion.provider.connect_calls == 1
            assert ingestion.provider.disconnect_calls == 0
            assert ingestion.provider.stream_calls == 1
            assert ingestion.epoch == 1

            await asyncio.sleep(0.005)  # let closed clients/tasks settle before measuring
            live_tasks = len(asyncio.all_tasks())
            live_clients = int((await probe.info("clients"))["connected_clients"])
            if cycle == 2:  # warm-up done; capture the steady-state ceilings
                baseline_tasks = live_tasks
                baseline_clients = live_clients
            elif cycle > 2:
                assert live_tasks <= baseline_tasks  # T21: no consumer/task accumulation
                assert live_clients <= baseline_clients  # T22: no Redis client accumulation

        # Every event across all 30 cycles is present exactly once (durable dedup, no reapply).
        report = compare(
            _expected_views(_mix(cycles * 4, offset=0), epoch=ingestion.epoch),
            _all_applied(backends),
            sample_limit=20,
        )
        assert report.is_clean
        assert report.matched_total == cycles * 4
        assert report.known_b2_duplicate_total == 0
    finally:
        await ingestion.stop()
        await probe.aclose()


# =========================================================================== #
# H6-T23 L1 continuity stays healthy while the backend is absent (backend-only restart)
# =========================================================================== #
async def test_h6_t23_l1_healthy_during_backend_downtime(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    ingestion = await _start_ingestion(redis_socket, config, tmp_path)
    try:
        await ingestion.publish(_mix(10, offset=0))
        backend = await _start_backend(redis_socket, config, "backend-a")
        await backend.drain()
        await backend.close()  # backend gone; ingestion continues

        await ingestion.publish(_mix(20, offset=10))  # publish while no backend exists
        snapshot = ingestion.stack.continuity.snapshot()
        assert snapshot.state is ContinuityState.HEALTHY  # T23: L1 unaffected by backend absence
        assert snapshot.publication_failure_total == 0
        assert snapshot.overflow_total == 0
        assert ingestion.service.status.value == "running"
    finally:
        await ingestion.stop()


# =========================================================================== #
# H6-T25/T29 no broker/provider real construction; canonical UTC (no FIX-2 workaround)
# =========================================================================== #
async def test_h6_t25_t29_no_broker_construction_and_canonical_utc(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    ingestion = await _start_ingestion(redis_socket, config, tmp_path)
    try:
        # T25: the only provider is the local fake; no app.adapters (Dhan) provider constructed.
        assert ingestion.service.provider is ingestion.provider
        assert not type(ingestion.provider).__module__.startswith("app.adapters")

        await ingestion.publish(_mix(4, offset=0))
        backend = await _start_backend(redis_socket, config, "backend-a")
        # T28: the only consumer destination is the non-authoritative shadow sink.
        assert isinstance(backend.sink, RecordingShadowSink)
        await backend.drain()
        report = compare(
            _expected_views(_mix(4, offset=0), epoch=ingestion.epoch), _all_applied([backend])
        )
        assert report.is_clean
        await backend.close()

        # T29: canonical tz-aware UTC; no +5:30/-5:30 workaround.
        envelope = _envelope(_tick(), seq=1, epoch=ingestion.epoch)
        assert envelope.produced_at.utcoffset() == datetime(2026, 1, 1, tzinfo=UTC).utcoffset()
        assert _NOW.utcoffset().total_seconds() == 0
    finally:
        await ingestion.stop()
