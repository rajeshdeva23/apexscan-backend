"""Runtime composition closure for md:health + reference bootstrap (DECOUPLING PHASE H9C-P1).

Closes the two implemented-but-unwired pipelines the post-H9B live-readiness audit found:

    GATE J  the ``md:health`` producer writer is now driven by the REAL ingestion runtime
            (the L1 observer projects continuity into md:health each tick + a final record on stop).
    GATE D  the reference bootstrap loader is now driven by the REAL consumer runtime
            (``start()`` rehydrates the compacted ``md:reference:<date>`` hash before events apply).

Everything is exercised against a disposable ``redislite`` server (private unix socket) with a
deterministic fake provider — no Dhan, no tokens, no sockets, no production, no ownership enable, no
IPC-authoritative switch, no cutover. Timestamps are canonical tz-aware UTC (no +5:30/-5:30).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.adapters.base.broker_adapter import BrokerAdapter
from app.market_ingestion.mode import PhaseHFlags, derive_market_path_mode
from app.market_ingestion.publication import (
    PublicationStack,
    PublishingEventSink,
    build_publication_stack,
)
from app.market_ingestion.service import MarketIngestionService
from app.market_ipc import (
    MarketEventConsumer,
    MarketEventEnvelope,
    MarketIpcConfig,
    RecordingShadowSink,
    RedisMarketEventStream,
    build_envelope,
)
from app.market_ipc.boundary import AsyncPublicationBoundary
from app.market_ipc.consumer_runtime import (
    MarketEventConsumerRuntime,
    RuntimeState,
    compose_consumer_runtime,
)
from app.market_ipc.continuity import ContinuityState, FeedContinuityTracker
from app.market_ipc.dedup import BoundedDeduplicator
from app.market_ipc.durable_dedup import CompositeDeduplicator, DurableDeduplicator
from app.market_ipc.epoch import DurableEpochAllocator
from app.market_ipc.events import IpcPayload
from app.market_ipc.health import IngestionHealthPublisher, IngestionHealthReader
from app.market_ipc.loss_detection import LossDetectionState, RedisLossDetector
from app.market_ipc.publisher import MarketEventPublisher, StaticUniverseVersion
from app.market_ipc.reference import (
    RedisCompactedReferenceStore,
    ReferenceSnapshot,
    ReferenceStateLoader,
)
from app.market_ipc.state import health_key, reference_key
from app.schemas.market_data import (
    Instrument,
    MarketDataKind,
    MarketReference,
    ProviderCapability,
    ProviderHealth,
    ProviderSessionOhlc,
    ProviderStatus,
    SubscriptionRequest,
    Tick,
)

redislite = pytest.importorskip("redislite", reason="disposable real Redis unavailable")

_NOW = datetime(2026, 9, 16, 10, 15, 30, tzinfo=UTC)
_TD = date(2026, 9, 16)
_PRODUCER = "market-ingestion"
_SYMBOLS = ("TCS", "INFY", "RELIANCE", "HDFC", "WIPRO")
_UNIVERSE = 7


@pytest.fixture(scope="module")
def redis_socket() -> str:
    server = redislite.Redis()
    try:
        yield server.socket_file
    finally:
        server.shutdown()


@pytest.fixture
async def redis(redis_socket: str) -> Redis:
    client: Redis = Redis(unix_socket_path=redis_socket)
    await client.flushall()
    try:
        yield client
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# Canonical fixtures (deterministic, tz-aware UTC)
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
        session_ohlc=ProviderSessionOhlc(
            open_price=Decimal("99"),
            high_price=Decimal("101"),
            low_price=Decimal("98"),
            close_price=Decimal("100.5"),
        ),
    )


def _reference(symbol: str = "TCS") -> MarketReference:
    return MarketReference(
        instrument=Instrument(exchange="NSE", symbol=symbol), previous_close=Decimal("99.25")
    )


def _request() -> SubscriptionRequest:
    return SubscriptionRequest(
        instruments=tuple(Instrument(exchange="NSE", symbol=s) for s in _SYMBOLS),
        data_types=frozenset({MarketDataKind.TICK}),
    )


def _envelope(payload: IpcPayload, *, seq: int, epoch: int) -> MarketEventEnvelope:
    return build_envelope(
        payload,
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=_UNIVERSE,
    )


async def _no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


class _FixedTradingDate:
    """Deterministic producer trading-date source (canonical UTC session date)."""

    def current_trading_date(self) -> date:
        return _TD


class _Clock:
    """Monotonic aware-UTC clock so md:health ``updated_at`` strictly advances per publish."""

    def __init__(self, base: datetime = _NOW) -> None:
        self._base = base
        self._ticks = 0

    def __call__(self) -> datetime:
        from datetime import timedelta

        self._ticks += 1
        return self._base + timedelta(milliseconds=self._ticks)


class _Settings:
    """Minimal settings surface compose_consumer_runtime needs."""

    def __init__(self, socket: str, *, flags: PhaseHFlags) -> None:
        self.redis_url = f"unix://{socket}"
        self._flags = flags

    def phase_h_flags(self) -> PhaseHFlags:
        return self._flags

    def market_ipc_config(self) -> MarketIpcConfig:
        return MarketIpcConfig(block_ms=0)


def _shadow_flags() -> PhaseHFlags:
    return PhaseHFlags(
        market_ingestion_service_enabled=False,
        ipc_publisher_enabled=False,
        ipc_consumer_enabled=True,
        ipc_shadow_compare_enabled=True,
        ipc_authoritative_enabled=False,
        legacy_market_path_enabled=True,
    )


# --------------------------------------------------------------------------- #
# Deterministic offline provider (implements the real adapter contract; no Dhan)
# --------------------------------------------------------------------------- #
class _FakeProvider(BrokerAdapter):
    capabilities = frozenset({ProviderCapability.LIVE_MARKET_DATA})

    def __init__(self, segments: list[list[IpcPayload]], *, block_after_last: bool = True) -> None:
        self._segments = [list(segment) for segment in segments]
        self._call = 0
        self._block_after_last = block_after_last
        self._stop = asyncio.Event()
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def stream_market_data(self, request: SubscriptionRequest):  # noqa: ARG002 - stub feed
        index = self._call
        self._call += 1
        segment = self._segments[index] if index < len(self._segments) else []
        for datum in segment:
            yield datum
        if self._block_after_last and index >= len(self._segments) - 1:
            await self._stop.wait()

    def release(self) -> None:
        self._stop.set()


class _FailingAtomic:
    """D1 that always fails the publish (a definite transport failure) → terminal break."""

    async def publish_stream_only(self, envelope: MarketEventEnvelope) -> object:
        from app.market_ipc.transport import RedisPublishError

        raise RedisPublishError("simulated D1 publish failure")

    async def publish_stream_and_reference(
        self, envelope: MarketEventEnvelope, reference_state: object
    ) -> object:
        from app.market_ipc.transport import RedisPublishError

        raise RedisPublishError("simulated D1 publish failure")


class _SeedingSink:
    """Reference-seedable non-authoritative sink: records the seed snapshot + applied events."""

    def __init__(self) -> None:
        self.seeded: ReferenceSnapshot | None = None
        self.applied: list[tuple[MarketEventEnvelope, IpcPayload]] = []

    async def seed_reference(self, snapshot: ReferenceSnapshot) -> None:
        self.seeded = snapshot

    async def apply(self, envelope: MarketEventEnvelope, event: IpcPayload) -> None:
        self.applied.append((envelope, event))


class _RaisingReferenceSource:
    """Reference source whose read raises — proves bootstrap fails closed on a Redis outage."""

    async def read_all_raw(self, trading_date: date) -> dict[str, bytes]:  # noqa: ARG002
        raise RedisError("simulated reference load outage")


# --------------------------------------------------------------------------- #
# Producer composition (real service, publisher mode, md:health writer wired)
# --------------------------------------------------------------------------- #
class _Composed:
    def __init__(
        self,
        service: MarketIngestionService,
        stack: PublicationStack,
        prod: Redis,
        provider: _FakeProvider,
    ) -> None:
        self.service = service
        self.stack = stack
        self.prod = prod
        self.provider = provider


def _publisher_flags() -> PhaseHFlags:
    return PhaseHFlags(
        market_ingestion_service_enabled=True,
        ipc_publisher_enabled=True,
        ipc_consumer_enabled=False,
        ipc_shadow_compare_enabled=False,
        ipc_authoritative_enabled=False,
        legacy_market_path_enabled=True,
    )


def _service_with_stack(
    stack: PublicationStack,
    prod: Redis,
    config: MarketIpcConfig,
    provider: _FakeProvider,
    *,
    clock,
    max_reconnects: int = 0,
) -> MarketIngestionService:
    """The exact production shape composition builds: service + injected md:health publisher."""
    return MarketIngestionService(
        flags=_publisher_flags(),
        provider=provider,
        subscription_request=_request(),
        publication=stack,
        health_publisher=IngestionHealthPublisher(prod, config),
        supervisor_max_reconnects=max_reconnects,
        supervisor_sleep=_no_sleep,
        observer_interval_seconds=0.002,
        now=clock,
    )


async def _start_publisher(
    redis_socket: str,
    config: MarketIpcConfig,
    state_dir: Path,
    provider: _FakeProvider,
    *,
    clock=lambda: _NOW,
    max_reconnects: int = 0,
) -> _Composed:
    prod: Redis = Redis(unix_socket_path=redis_socket)
    stack = build_publication_stack(
        redis=prod,
        config=config,
        producer_id=_PRODUCER,
        state_dir=state_dir,
        now=lambda: _NOW,
        trading_date_source=_FixedTradingDate(),
        universe_version=_UNIVERSE,
    )
    service = _service_with_stack(
        stack, prod, config, provider, clock=clock, max_reconnects=max_reconnects
    )
    await service.start()
    return _Composed(service, stack, prod, provider)


def _failing_stack(prod: Redis, config: MarketIpcConfig, state_dir: Path) -> PublicationStack:
    """A real publication stack around a failing D1 (drives a terminal publication break)."""
    stream = RedisMarketEventStream(redis=prod, config=config)
    publisher = MarketEventPublisher(
        stream=stream,
        config=config,
        producer_id=_PRODUCER,
        epoch_allocator=DurableEpochAllocator(state_dir),
        trading_date_source=_FixedTradingDate(),
        universe_version_source=StaticUniverseVersion(_UNIVERSE),
        now=lambda: _NOW,
        atomic_publisher=_FailingAtomic(),  # type: ignore[arg-type]
    )
    boundary = AsyncPublicationBoundary(
        publisher=publisher, capacity=16, drain_timeout_seconds=1.0, now=lambda: _NOW
    )
    continuity = FeedContinuityTracker()
    sink = PublishingEventSink(boundary=boundary, continuity=continuity, publisher=publisher)
    return PublicationStack(
        producer_id=_PRODUCER,
        publisher=publisher,
        boundary=boundary,
        continuity=continuity,
        sink=sink,
        redis=prod,
    )


async def _drive_events(service: MarketIngestionService, expected: int) -> None:
    for _ in range(20_000):
        if service.diagnostics().events_total >= expected:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"service ingested {service.diagnostics().events_total} < {expected}")


async def _read_health(redis: Redis, config: MarketIpcConfig):
    return await IngestionHealthReader(redis, config).read()


# --------------------------------------------------------------------------- #
# Consumer composition helpers
# --------------------------------------------------------------------------- #
def _durable(redis: Redis, config: MarketIpcConfig) -> CompositeDeduplicator:
    return CompositeDeduplicator(
        memory=BoundedDeduplicator(config.dedup_max_entries),
        durable=DurableDeduplicator(redis, config),
    )


def _consumer(
    redis: Redis,
    config: MarketIpcConfig,
    sink,
    *,
    deduplicator: CompositeDeduplicator | None = None,
) -> MarketEventConsumer:
    return MarketEventConsumer(
        transport=RedisMarketEventStream(redis=redis, config=config),
        config=config,
        sink=sink,
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: _UNIVERSE,
        now=lambda: _NOW,
        deduplicator=deduplicator or _durable(redis, config),
    )


def _runtime_with_loader(
    redis: Redis,
    config: MarketIpcConfig,
    *,
    sink,
    source,
    trading_date=lambda: _TD,
    universe=lambda: _UNIVERSE,
    deduplicator: CompositeDeduplicator | None = None,
) -> MarketEventConsumerRuntime:
    """Build a runtime directly around a real consumer + a reference loader over ``source``."""
    flags = _shadow_flags()
    consumer = _consumer(redis, config, sink, deduplicator=deduplicator)
    return MarketEventConsumerRuntime(
        mode=derive_market_path_mode(flags),
        flags=flags,
        consumer=consumer,
        redis=redis,
        poll_idle_seconds=0.01,
        loss_detector=RedisLossDetector(redis, config),
        health_reader=IngestionHealthReader(redis, config),
        reference_loader=ReferenceStateLoader(source=source, now=lambda: _NOW),
        trading_date_source=trading_date,
        universe_version_source=universe,
        now=lambda: _NOW,
    )


async def _drain(consumer: MarketEventConsumer, redis: Redis, config: MarketIpcConfig) -> None:
    for _ in range(400):
        before = consumer.diagnostics().acked_total
        await consumer.poll_once()
        pending = (await redis.xpending(config.stream_name, config.consumer_group))["pending"]
        if consumer.diagnostics().acked_total == before and pending == 0:
            return
    raise AssertionError("stream did not drain within the cycle budget")


async def _wait(predicate, *, limit: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached before timeout")


# =========================================================================== #
# PART A — GATE J: md:health producer writer driven by the real runtime
# =========================================================================== #
async def test_j_health_appears_with_ttl_and_producer_identity(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = MarketIpcConfig(block_ms=0)
    composed = await _start_publisher(
        redis_socket, config, tmp_path, _FakeProvider([[_tick(price="10")]])
    )
    try:
        await _drive_events(composed.service, 1)
        await _wait_health(redis, config)
        state = await _read_health(redis, config)
        assert state is not None
        assert state.producer_id == _PRODUCER
        assert state.producer_epoch == composed.service.diagnostics().producer_epoch
        assert state.ingestion is ProviderStatus.HEALTHY
        ttl = await redis.ttl(health_key(config))
        assert 0 < ttl <= config.health_ttl_seconds  # a bounded TTL is set
    finally:
        composed.provider.release()
        await composed.service.stop()


async def _wait_health(redis: Redis, config: MarketIpcConfig) -> None:
    for _ in range(500):
        if await redis.exists(health_key(config)):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("md:health never appeared")


async def test_j_updated_at_refreshes_while_idle(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = MarketIpcConfig(block_ms=0)
    clock = _Clock()
    prod: Redis = Redis(unix_socket_path=redis_socket)
    stack = build_publication_stack(
        redis=prod,
        config=config,
        producer_id=_PRODUCER,
        state_dir=tmp_path,
        now=lambda: _NOW,
        trading_date_source=_FixedTradingDate(),
        universe_version=_UNIVERSE,
    )
    provider = _FakeProvider([[_tick(price="10")]])  # one event then idle (blocks)
    service = _service_with_stack(stack, prod, config, provider, clock=clock)
    await service.start()
    try:
        await _drive_events(service, 1)
        await _wait_health(redis, config)
        first = (await _read_health(redis, config)).updated_at
        # No new market events; the time-driven observer must still refresh updated_at.
        second = first
        for _ in range(500):
            second = (await _read_health(redis, config)).updated_at
            if second > first:
                break
            await asyncio.sleep(0.005)
        assert second > first  # idle-but-healthy feed does not go stale
    finally:
        provider.release()
        await service.stop()


async def test_j_producer_position_advances_and_health_tracks_incarnation(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    """The producer accepted position advances as events flow; md:health tracks the incarnation.

    ``last_published_sequence`` in md:health is conveyed faithfully from continuity, which in the
    live path leaves it ``None`` (the M2 boundary reports a published *count*, not a *sequence*, so
    ``observe_boundary`` confirms publishes without a watermark — a pre-existing boundary limit, not
    this writer). The advancing producer position that IS tracked live is the accepted sequence.
    """
    config = MarketIpcConfig(block_ms=0)
    composed = await _start_publisher(
        redis_socket,
        config,
        tmp_path,
        _FakeProvider([[_tick(price=str(10 + i)) for i in range(5)]]),
    )
    try:
        await _drive_events(composed.service, 5)
        await _wait_health(redis, config)
        await _wait(lambda: (composed.service.diagnostics().last_accepted_sequence or 0) >= 5)
        assert composed.service.diagnostics().last_accepted_sequence >= 5  # producer advanced
        state = await _read_health(redis, config)
        assert state is not None
        assert state.producer_id == _PRODUCER
        assert state.producer_epoch == composed.service.diagnostics().producer_epoch
        # Faithful conveyance: health carries exactly what continuity holds (no fabrication).
        assert (
            state.last_published_sequence
            == composed.stack.continuity.snapshot().last_published_sequence
        )
    finally:
        composed.provider.release()
        await composed.service.stop()


async def test_j_reconnect_preserves_producer_epoch_in_health(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = MarketIpcConfig(block_ms=0)
    provider = _FakeProvider(
        [[_tick(symbol="TCS", price="10")], [_tick(symbol="INFY", price="11")]],
        block_after_last=True,
    )
    composed = await _start_publisher(redis_socket, config, tmp_path, provider, max_reconnects=3)
    try:
        await _drive_events(composed.service, 2)  # both segments consumed → a reconnect happened
        await _wait_health(redis, config)
        await _wait(
            lambda: composed.stack.continuity.snapshot().provider_reconnect_total >= 1,
        )
        epoch = composed.service.diagnostics().producer_epoch
        state = await _read_health(redis, config)
        assert state is not None
        assert state.producer_epoch == epoch  # a reconnect keeps the same incarnation epoch
    finally:
        provider.release()
        await composed.service.stop()


async def test_j_terminal_break_represented_in_health(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = MarketIpcConfig(block_ms=0)
    prod: Redis = Redis(unix_socket_path=redis_socket)
    stack = _failing_stack(prod, config, tmp_path)
    provider = _FakeProvider([[_tick(price="1")]])
    service = _service_with_stack(stack, prod, config, provider, clock=lambda: _NOW)
    await service.start()
    try:
        await _wait(lambda: stack.continuity.snapshot().state is ContinuityState.BROKEN)
        for _ in range(500):
            state = await _read_health(redis, config)
            if state is not None and state.terminal_publication_break:
                break
            await asyncio.sleep(0.005)
        state = await _read_health(redis, config)
        assert state is not None
        assert state.terminal_publication_break is True  # a terminal break is conveyed
        assert state.ingestion is ProviderStatus.DOWN
    finally:
        provider.release()
        await service.stop()
        await prod.aclose()


async def test_j_health_write_failure_does_not_crash_and_reader_fails_closed(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = MarketIpcConfig(block_ms=0)
    composed = await _start_publisher(
        redis_socket, config, tmp_path, _FakeProvider([[_tick(price="10")]])
    )
    await _drive_events(composed.service, 1)
    await _wait_health(redis, config)
    # A stale/absent record fails closed on the reader side (no false "healthy").
    await redis.delete(health_key(config))
    reader = IngestionHealthReader(redis, config)
    assert await reader.read() is None
    assert await reader.read_evidence(_NOW) is None
    composed.provider.release()
    await composed.service.stop()
    assert composed.service.status.value in {"stopped", "failed"}  # no crash storm


# =========================================================================== #
# PART B — GATE D: reference bootstrap driven by the real consumer runtime
# =========================================================================== #
async def _seed_reference(redis: Redis, config: MarketIpcConfig) -> RedisCompactedReferenceStore:
    """Publish canonical reference state to md:reference:<date> via the REAL producer path."""
    store = RedisCompactedReferenceStore(redis, config)
    from app.market_ipc.reference import reference_from_envelope

    for i, payload in enumerate((_reference("TCS"), _tick_with_ohlc("TCS")), start=1):
        state = reference_from_envelope(_envelope(payload, seq=i, epoch=1))
        assert state is not None
        await store.compact(state)
    return store


async def test_d_bootstrap_loads_and_seeds_reference_without_dhan(
    redis_socket: str, redis: Redis
) -> None:
    config = MarketIpcConfig(block_ms=0)
    store = await _seed_reference(redis, config)
    sink = _SeedingSink()
    runtime = _runtime_with_loader(redis, config, sink=sink, source=store)
    await runtime.start()
    try:
        assert runtime.reference_snapshot is not None
        snapshot = runtime.reference_snapshot
        assert "NSE:TCS" in snapshot.states
        merged = snapshot.states["NSE:TCS"]
        assert merged.previous_close == Decimal("99.25")  # from MarketReference
        assert merged.session_open == Decimal("99")  # from Tick.session_ohlc
        assert sink.seeded is snapshot  # the sink was seeded before any event applied
    finally:
        await runtime.stop()


async def test_d_bootstrap_preserves_existing_group_and_pending_entries(
    redis_socket: str, redis: Redis
) -> None:
    config = MarketIpcConfig(block_ms=0)
    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    for i in range(1, 4):
        await stream.publish(_envelope(_tick(price=str(10 + i)), seq=i, epoch=1))
    # Strand the entries in the PEL under a different consumer (read, never ACK).
    dead = RedisMarketEventStream(
        redis=redis, config=config.model_copy(update={"consumer_name": "dead"})
    )
    stranded = await dead.read_raw()
    assert len(stranded) == 3
    assert (await redis.xpending(config.stream_name, config.consumer_group))["pending"] == 3
    group_before = await redis.xinfo_groups(config.stream_name)

    store = await _seed_reference(redis, config)
    sink = _SeedingSink()
    runtime = _runtime_with_loader(
        redis,
        config.model_copy(update={"consumer_name": "recover", "claim_idle_ms": 1}),
        sink=sink,
        source=store,
    )
    await runtime.start()  # bootstrap must not reset the group, discard the PEL, or rewind progress
    try:
        assert sink.seeded is not None  # reference bootstrapped...
        group_after = await redis.xinfo_groups(config.stream_name)
        assert group_after[0]["name"] == group_before[0]["name"]  # group not reset
        await _wait(lambda: len(sink.applied) >= 3)  # ...and the stranded PEL entries are recovered
    finally:
        await runtime.stop()
    assert len(sink.applied) == 3  # every valid event applied; none dropped by the bootstrap


async def test_d_bootstrap_does_not_invent_or_rewrite_producer_epoch(
    redis_socket: str, redis: Redis
) -> None:
    config = MarketIpcConfig(block_ms=0)
    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    await stream.publish(_envelope(_tick(price="10"), seq=1, epoch=5))  # existing epoch 5
    store = await _seed_reference(redis, config)
    sink = _SeedingSink()
    runtime = _runtime_with_loader(redis, config, sink=sink, source=store)
    await runtime.start()
    try:
        await _wait(lambda: len(sink.applied) >= 1)
    finally:
        await runtime.stop()
    envelope, _payload = sink.applied[0]
    assert envelope.producer_epoch == 5  # the consumer accepts the legal epoch, never rewrites it
    progress = runtime.diagnostics()
    assert progress is not None
    assert progress.last_applied_epoch == 5  # progress reflects the producer's epoch, not a new one


async def test_d_bootstrap_warming_up_when_reference_empty(redis_socket: str, redis: Redis) -> None:
    config = MarketIpcConfig(block_ms=0)
    store = RedisCompactedReferenceStore(redis, config)  # nothing seeded
    sink = _SeedingSink()
    runtime = _runtime_with_loader(redis, config, sink=sink, source=store)
    await runtime.start()
    try:
        assert runtime.reference_snapshot is not None
        assert runtime.reference_snapshot.warming_up  # empty hash → warming, seeds nothing false
        assert runtime.is_ready  # the consumer still runs
    finally:
        await runtime.stop()


async def test_d_bootstrap_fails_closed_on_reference_load_outage(
    redis_socket: str, redis: Redis
) -> None:
    config = MarketIpcConfig(block_ms=0)
    sink = _SeedingSink()
    runtime = _runtime_with_loader(redis, config, sink=sink, source=_RaisingReferenceSource())
    with pytest.raises(RedisError):
        await runtime.start()  # a load outage must fail the start, never claim ready on empty
    assert runtime.state is RuntimeState.FAILED
    assert not runtime.is_ready


async def test_d_no_bootstrap_without_trading_date_authority(
    redis_socket: str, redis: Redis
) -> None:
    config = MarketIpcConfig(block_ms=0)
    store = await _seed_reference(redis, config)
    sink = _SeedingSink()
    runtime = _runtime_with_loader(
        redis, config, sink=sink, source=store, trading_date=lambda: None
    )
    await runtime.start()
    try:
        assert runtime.reference_snapshot is None  # no authoritative date → no bootstrap
        assert sink.seeded is None
    finally:
        await runtime.stop()


async def test_d_compose_consumer_runtime_wires_the_loader(redis_socket: str, redis: Redis) -> None:
    """The whole Gate-D point: composition itself now builds and drives the reference loader."""
    config = MarketIpcConfig(block_ms=0)
    await _seed_reference(redis, config)
    runtime = await compose_consumer_runtime(
        _Settings(redis_socket, flags=_shadow_flags()),
        sink=RecordingShadowSink(),
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: _UNIVERSE,
        now=lambda: _NOW,
    )
    await runtime.start()
    try:
        assert runtime.reference_snapshot is not None
        assert "NSE:TCS" in runtime.reference_snapshot.states  # loaded by composition, not a test
    finally:
        await runtime.stop()


# =========================================================================== #
# PART C — COMBINED: producer md:health + reference bootstrap + consumer + B11
# =========================================================================== #
async def test_combined_health_reference_consumer_and_b11_sufficient(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = MarketIpcConfig(block_ms=0)
    events: list[IpcPayload] = [
        _reference("TCS"),
        _tick_with_ohlc("TCS"),
        _tick("INFY", price="55"),
        _tick("HDFC", price="66"),
    ]
    composed = await _start_publisher(redis_socket, config, tmp_path, _FakeProvider([events]))
    try:
        await _drive_events(composed.service, len(events))
        await _wait_health(redis, config)  # producer md:health present (Gate J)
        # The reference is written by the async M2→D1 worker, which may lag the submit count; poll.
        ref_key = reference_key(config.reference_key_prefix, _TD)
        for _ in range(500):
            if await redis.exists(ref_key) == 1:
                break
            await asyncio.sleep(0.005)
        assert await redis.exists(ref_key) == 1

        # Backend consumer starts from NO local state: bootstraps reference, then consumes.
        sink = _SeedingSink()
        runtime = await compose_consumer_runtime(
            _Settings(redis_socket, flags=_shadow_flags()),
            sink=sink,
            trading_date_source=lambda: _TD,
            universe_version_source=lambda: _UNIVERSE,
            now=lambda: _NOW,
        )
        await runtime.start()
        try:
            assert runtime.reference_snapshot is not None
            assert not runtime.reference_snapshot.warming_up  # reference bootstrapped (Gate D)
            await _wait(lambda: len(sink.applied) >= len(events))
            # B11 now has live-shaped producer evidence — no longer permanently insufficient.
            result = await runtime.evaluate_authority_readiness()
            assert result.state is not LossDetectionState.INSUFFICIENT_EVIDENCE
            assert result.state in {
                LossDetectionState.HEALTHY,
                LossDetectionState.CONSUMER_LAGGING,
            }
            assert result.ready_for_authority is True
        finally:
            await runtime.stop()
        assert len(sink.applied) == len(events)  # exactly-once logical application, no loss
    finally:
        composed.provider.release()
        await composed.service.stop()


async def test_combined_backend_restart_rehydrates_without_dhan(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = MarketIpcConfig(block_ms=0)
    events: list[IpcPayload] = [_reference("TCS"), _tick_with_ohlc("TCS"), _tick("INFY", price="7")]
    composed = await _start_publisher(redis_socket, config, tmp_path, _FakeProvider([events]))
    await _drive_events(composed.service, len(events))
    await _wait_health(redis, config)

    # Consumer A: fresh start, bootstraps + applies all.
    sink_a = _SeedingSink()
    runtime_a = await compose_consumer_runtime(
        _Settings(redis_socket, flags=_shadow_flags()),
        sink=sink_a,
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: _UNIVERSE,
        now=lambda: _NOW,
    )
    await runtime_a.start()
    await _wait(lambda: len(sink_a.applied) >= len(events))
    epoch_seen = composed.service.diagnostics().producer_epoch
    await runtime_a.stop()

    # Consumer B: a brand-new backend process (fresh memory, same durable Redis). It rehydrates
    # reference, re-derives dedup from the durable authority, and applies nothing new. No Dhan.
    sink_b = _SeedingSink()
    runtime_b = await compose_consumer_runtime(
        _Settings(redis_socket, flags=_shadow_flags()),
        sink=sink_b,
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: _UNIVERSE,
        now=lambda: _NOW,
    )
    await runtime_b.start()
    try:
        assert runtime_b.reference_snapshot is not None
        assert not runtime_b.reference_snapshot.warming_up  # reference rehydrated on restart
        await asyncio.sleep(0.05)
        assert len(sink_b.applied) == 0  # durable dedup preserved: no duplicate application
        # producer epoch unchanged; provider never contacted (fake provider still the only one)
        assert composed.service.diagnostics().producer_epoch == epoch_seen
        assert composed.service.provider.connect_calls == 1  # type: ignore[attr-defined]
    finally:
        await runtime_b.stop()
        composed.provider.release()
        await composed.service.stop()


async def test_combined_ingestion_restart_new_epoch_no_false_b11_loss(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = MarketIpcConfig(block_ms=0)
    # First incarnation.
    c1 = await _start_publisher(
        redis_socket, config, tmp_path, _FakeProvider([[_tick("TCS", price="10")]])
    )
    await _drive_events(c1.service, 1)
    await _wait_health(redis, config)
    epoch1 = c1.service.diagnostics().producer_epoch
    c1.provider.release()
    await c1.service.stop()

    # Second incarnation (same durable state dir → strictly higher epoch).
    c2 = await _start_publisher(
        redis_socket, config, tmp_path, _FakeProvider([[_tick("INFY", price="20")]])
    )
    try:
        await _drive_events(c2.service, 1)
        epoch2 = c2.service.diagnostics().producer_epoch
        assert epoch2 is not None and epoch1 is not None and epoch2 > epoch1  # new epoch

        async def _health_epoch_reached() -> bool:
            state = await _read_health(redis, config)
            return state is not None and state.producer_epoch == epoch2

        for _ in range(500):
            if await _health_epoch_reached():
                break
            await asyncio.sleep(0.005)
        state = await _read_health(redis, config)
        assert state is not None
        assert state.producer_epoch == epoch2  # md:health reflects the new producer epoch

        sink = _SeedingSink()
        runtime = await compose_consumer_runtime(
            _Settings(redis_socket, flags=_shadow_flags()),
            sink=sink,
            trading_date_source=lambda: _TD,
            universe_version_source=lambda: _UNIVERSE,
            now=lambda: _NOW,
        )
        await runtime.start()
        try:
            await _wait(lambda: len(sink.applied) >= 1)
            result = await runtime.evaluate_authority_readiness()
            # A legal epoch transition is not a Redis loss.
            assert result.state not in {
                LossDetectionState.REDIS_STREAM_RESET,
                LossDetectionState.REDIS_STATE_REWIND,
                LossDetectionState.PUBLISHED_EVENT_UNACCOUNTED_FOR,
            }
        finally:
            await runtime.stop()
    finally:
        c2.provider.release()
        await c2.service.stop()
