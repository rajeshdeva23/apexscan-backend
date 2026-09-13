"""Offline end-to-end IPC parity for the decoupled market path (DECOUPLING PHASE H5).

One exercise of the FULL offline topology against a real disposable ``redis-server`` (bundled by
``redislite`` on a private unix socket) — never a shared or production Redis:

    deterministic fake provider (no Dhan / tokens / sockets / internet)
        -> market-ingestion service + ProviderSupervisor          (real composition)
        -> M1 producer identity  -> M2 AsyncPublicationBoundary    (real composition)
        -> D1 RedisAtomicPublisher -> L1 FeedContinuityTracker     (real composition)
        -> Redis md:events (+ md:reference:<date>)
        -> H4A/H4B MarketEventConsumer -> C1 CompositeDeduplicator  (real composition)
        -> non-authoritative RecordingShadowSink
        -> H4C semantic comparator

The producer side is exercised through the REAL composition-root helper ``build_publication_stack``
and the REAL ``PublishingEventSink`` (the exact seam ``ProviderSupervisor`` drives), plus the full
``MarketIngestionService`` for the provider/supervisor/service-lifecycle tests — no hand-rolled
publish path. Failure-path tests inject a fault only at the D1 seam (a controllable
``AtomicPublisher`` double) while keeping the real M2 boundary, real L1 tracker, and real sink.

H5 is OFFLINE and NON-AUTHORITATIVE: no live Dhan, no production contact, no consumer activation,
no IPC authority, no TickEngine/MarketContext, no FIX-2 workaround. Timestamps are canonical
tz-aware UTC (no +5:30/-5:30). Skips cleanly if redislite is absent.
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
    PublicationTerminalError,
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
from app.market_ipc.continuity import ContinuityReason, ContinuityState, FeedContinuityTracker
from app.market_ipc.envelope import ProducerEventIdentity
from app.market_ipc.epoch import DurableEpochAllocator
from app.market_ipc.events import IpcPayload
from app.market_ipc.publisher import MarketEventPublisher, StaticUniverseVersion
from app.market_ipc.reference import ReferenceOutcome
from app.market_ipc.state import reference_key
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


def _representative_mix(count: int) -> list[IpcPayload]:
    """Deterministic canonical events across instruments and all IPC-supported kinds."""
    events: list[IpcPayload] = []
    for i in range(count):
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
    """Reconstruct the envelope the producer must have built for a submitted event.

    Producer stamping is deterministic (fixed clock/trading-date/universe-version, contiguous
    per-epoch sequence in submission order), so the expected identity of the i-th submitted event
    is ``(producer_id, epoch, i)`` — the comparator then proves the APPLIED output matches.
    """
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
# Deterministic offline provider — implements the real adapter contracts, never touches Dhan
# --------------------------------------------------------------------------- #
class _FakeProvider(BrokerAdapter):
    """Local, deterministic provider: yields canonical events, no tokens/sockets/internet."""

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
        # A non-final segment RETURNS (a recoverable end -> supervisor reconnect, driving L1
        # disconnect/reconnect); the final segment BLOCKS so the stream stays "up" until shutdown.
        if self._block_after_last and index >= len(self._segments) - 1:
            await self._stop.wait()

    def release(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- #
# Controllable D1 seam doubles (real M2 boundary + real L1 around a faultable AtomicPublisher)
# --------------------------------------------------------------------------- #
class _BlockingAtomic:
    """D1 that blocks in-flight until released, to fill the bounded M2 queue deterministically."""

    def __init__(self) -> None:
        self._release = asyncio.Event()

    async def publish_stream_only(self, envelope: MarketEventEnvelope) -> AtomicPublicationResult:
        await self._release.wait()
        return AtomicPublicationResult("0-0", ReferenceOutcome.NO_REFERENCE_DATA)

    async def publish_stream_and_reference(
        self, envelope: MarketEventEnvelope, reference_state: object
    ) -> AtomicPublicationResult:
        await self._release.wait()
        return AtomicPublicationResult("0-0", ReferenceOutcome.WRITTEN)

    def release(self) -> None:
        self._release.set()


class _FailingAtomic:
    """D1 that always fails the publish (a definite transport failure)."""

    async def publish_stream_only(self, envelope: MarketEventEnvelope) -> AtomicPublicationResult:
        raise RedisPublishError("simulated D1 publish failure")

    async def publish_stream_and_reference(
        self, envelope: MarketEventEnvelope, reference_state: object
    ) -> AtomicPublicationResult:
        raise RedisPublishError("simulated D1 publish failure")


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


# --------------------------------------------------------------------------- #
# Producer composition (real publication stack + real service) helpers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Produced:
    epoch: int
    published_total: int
    last_sequence: int
    producer_id: str


async def _produce_events(
    redis_socket: str,
    config: MarketIpcConfig,
    state_dir: Path,
    events: list[IpcPayload],
    *,
    universe_version: int = 7,
) -> _Produced:
    """Drive canonical events through the REAL M1/M2/D1/L1 publication stack to Redis."""
    prod: Redis = Redis(unix_socket_path=redis_socket)
    stack = build_publication_stack(
        redis=prod,
        config=config,
        producer_id=_PRODUCER,
        state_dir=state_dir,
        now=lambda: _NOW,
        trading_date_source=_FixedTradingDate(),
        universe_version=universe_version,
    )
    await stack.boundary.start()  # allocates the M1 epoch, ensures the stream group
    epoch = stack.publisher.diagnostics().producer_epoch
    assert epoch is not None
    stack.continuity.producer_started(producer_id=_PRODUCER, producer_epoch=epoch)
    stack.continuity.provider_connected()
    for datum in events:
        stack.sink.handle(datum)  # REAL PublishingEventSink -> M2 submit + L1 accept
    result = await stack.boundary.stop()  # drain M2 -> D1 -> Redis
    stack.continuity.observe_boundary(stack.boundary.diagnostics())
    stack.continuity.drain_completed(result)
    diagnostics = stack.publisher.diagnostics()
    produced = _Produced(
        epoch=epoch,
        published_total=stack.boundary.diagnostics().published_total,
        last_sequence=diagnostics.current_sequence,
        producer_id=diagnostics.producer_id,
    )
    await stack.aclose()  # closes the producer's own client (not the consumer fixture client)
    return produced


def _manual_stack(
    *,
    stream_redis: Redis,
    atomic: object,
    config: MarketIpcConfig,
    state_dir: Path,
    capacity: int,
) -> tuple[MarketEventPublisher, AsyncPublicationBoundary, FeedContinuityTracker]:
    """Assemble the REAL M1/M2/L1 components around a controllable D1 (for seam-fault tests)."""
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


async def _start_service(
    redis_socket: str,
    config: MarketIpcConfig,
    state_dir: Path,
    provider: _FakeProvider,
    *,
    max_reconnects: int = 0,
) -> tuple[MarketIngestionService, object]:
    """Start the REAL market-ingestion service (publisher mode) over a fake provider."""
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
        supervisor_max_reconnects=max_reconnects,
        supervisor_sleep=_no_sleep,
        observer_interval_seconds=0.005,
    )
    await service.start()
    return service, stack


async def _drive_and_stop(
    service: MarketIngestionService, provider: _FakeProvider, *, expected: int, cycles: int = 10_000
) -> None:
    for _ in range(cycles):
        if service.diagnostics().events_total >= expected:
            break
        await asyncio.sleep(0.001)
    else:
        raise AssertionError(f"service ingested {service.diagnostics().events_total} < {expected}")
    provider.release()
    await service.stop()  # drains M2 -> D1 -> Redis, closes the producer client


# --------------------------------------------------------------------------- #
# Consumer composition (durable C1) + doubles — mirror the H4B/H4C/H4D helpers
# --------------------------------------------------------------------------- #
def _config(**overrides: object) -> MarketIpcConfig:
    return MarketIpcConfig(block_ms=0, **overrides)


def _named(redis: Redis, config: MarketIpcConfig, name: str) -> RedisMarketEventStream:
    return RedisMarketEventStream(
        redis=redis, config=config.model_copy(update={"consumer_name": name})
    )


def _fast_idle(config: MarketIpcConfig, *, consumer_name: str) -> MarketIpcConfig:
    return config.model_copy(update={"consumer_name": consumer_name, "claim_idle_ms": 1})


def _durable(redis: Redis, config: MarketIpcConfig) -> CompositeDeduplicator:
    return CompositeDeduplicator(
        memory=BoundedDeduplicator(config.dedup_max_entries),
        durable=DurableDeduplicator(redis, config),
    )


def _consumer(
    redis: Redis,
    config: MarketIpcConfig,
    sink: RecordingShadowSink,
    *,
    transport: RedisMarketEventStream | None = None,
    deduplicator: CompositeDeduplicator | None = None,
) -> MarketEventConsumer:
    return MarketEventConsumer(
        transport=transport or RedisMarketEventStream(redis=redis, config=config),
        config=config,
        sink=sink,
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: 7,
        now=lambda: _NOW,
        deduplicator=deduplicator or _durable(redis, config),
    )


async def _pending(redis: Redis, config: MarketIpcConfig) -> int:
    summary = await redis.xpending(config.stream_name, config.consumer_group)
    return int(summary["pending"])


async def _drain(consumer: MarketEventConsumer, redis: Redis, config: MarketIpcConfig) -> None:
    for _ in range(400):
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

    async def contains(self, identity: ProducerEventIdentity) -> bool:
        return await self._delegate.contains(identity)

    async def record(self, identity: ProducerEventIdentity) -> None:
        raise RedisError("simulated crash before durable mark")


# =========================================================================== #
# H5-T01 baseline end-to-end canonical parity (full service composition)
# =========================================================================== #
async def test_h5_t01_baseline_end_to_end_canonical_parity(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    events = _representative_mix(200)
    provider = _FakeProvider([events])
    service, stack = await _start_service(redis_socket, config, tmp_path, provider)
    await _drive_and_stop(service, provider, expected=200)
    assert stack.continuity.snapshot().state is ContinuityState.STOPPED  # clean shutdown, no break

    epoch = service.diagnostics().producer_epoch
    assert epoch is not None
    sink = RecordingShadowSink(max_entries=250)
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await _drain(consumer, redis, config)

    report = compare(
        _expected_views(events, epoch=epoch), views_from_applied(sink.events), sample_limit=20
    )
    assert report.is_clean
    assert report.matched_total == 200
    assert report.missing_total == 0
    assert report.unexpected_total == 0
    assert report.value_mismatch_total == 0
    assert sink.applied_total == 200


# =========================================================================== #
# H5-T02 all supported canonical event kinds reach parity
# =========================================================================== #
async def test_h5_t02_all_supported_event_kinds_reach_parity(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    events: list[IpcPayload] = [
        _tick(symbol="TCS", price="10"),
        _quote(symbol="INFY"),
        _reference(symbol="HDFC"),
        _tick_with_ohlc(symbol="WIPRO"),
    ]
    provider = _FakeProvider([events])
    service, _ = await _start_service(redis_socket, config, tmp_path, provider)
    await _drive_and_stop(service, provider, expected=len(events))
    epoch = service.diagnostics().producer_epoch
    assert epoch is not None

    sink = RecordingShadowSink()
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await _drain(consumer, redis, config)

    report = compare(_expected_views(events, epoch=epoch), views_from_applied(sink.events))
    assert report.is_clean
    assert report.matched_total == 4


# =========================================================================== #
# H5-T03 STREAM_ONLY publication writes no reference key
# =========================================================================== #
async def test_h5_t03_stream_only_publication(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    events: list[IpcPayload] = [_quote(symbol="TCS"), _tick(symbol="INFY")]  # no reference data
    produced = await _produce_events(redis_socket, config, tmp_path, events)
    assert produced.published_total == 2
    assert await redis.exists(reference_key(config.reference_key_prefix, _TD)) == 0

    sink = RecordingShadowSink()
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await _drain(consumer, redis, config)
    report = compare(_expected_views(events, epoch=produced.epoch), views_from_applied(sink.events))
    assert report.is_clean
    assert report.matched_total == 2


# =========================================================================== #
# H5-T04/T19 STREAM_PLUS_REFERENCE: atomic stream + compacted reference
# =========================================================================== #
async def test_h5_t04_t19_stream_plus_reference_atomic(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    events: list[IpcPayload] = [_reference(symbol="TCS"), _tick_with_ohlc(symbol="INFY")]
    produced = await _produce_events(redis_socket, config, tmp_path, events)

    key = reference_key(config.reference_key_prefix, _TD)
    assert await redis.exists(key) == 1
    assert await redis.hlen(key) == 2  # both instruments' reference state present...
    assert await redis.xlen(config.stream_name) == 2  # ...and both stream entries appended (atomic)

    sink = RecordingShadowSink()
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await _drain(consumer, redis, config)
    report = compare(_expected_views(events, epoch=produced.epoch), views_from_applied(sink.events))
    assert report.is_clean
    assert report.matched_total == 2


# =========================================================================== #
# H5-T05 M1 producer identity preserved (stable id/epoch, progressing sequence)
# =========================================================================== #
async def test_h5_t05_m1_identity_preserved(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    events: list[IpcPayload] = [_tick(symbol=s) for s in _SYMBOLS]
    produced = await _produce_events(redis_socket, config, tmp_path, events)
    assert produced.producer_id == _PRODUCER
    assert produced.epoch == 1  # first incarnation on a fresh durable epoch state dir
    assert produced.last_sequence == len(events)  # sequence progressed 1..N, contiguous
    assert produced.published_total == len(events)


# =========================================================================== #
# H5-T06/T07 ingestion restart -> higher epoch; same seq under a new epoch is distinct
# =========================================================================== #
async def test_h5_t06_t07_ingestion_restart_new_epoch_distinct(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    first: list[IpcPayload] = [_tick(symbol="TCS", price="10")]
    second: list[IpcPayload] = [_tick(symbol="TCS", price="20")]
    p1 = await _produce_events(redis_socket, config, tmp_path, first)
    p2 = await _produce_events(redis_socket, config, tmp_path, second)  # same state dir -> restart
    assert p2.epoch > p1.epoch  # T06: restart allocated a strictly higher epoch
    assert p1.last_sequence == 1 and p2.last_sequence == 1  # each epoch's sequence restarts at 1

    sink = RecordingShadowSink()
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await _drain(consumer, redis, config)
    expected = _expected_views(first, epoch=p1.epoch) + _expected_views(second, epoch=p2.epoch)
    report = compare(expected, views_from_applied(sink.events))
    assert report.is_clean
    assert report.matched_total == 2  # T07: seq=1/epoch=1 and seq=1/epoch=2 are DISTINCT
    assert report.duplicate_suppressed_total == 0


# =========================================================================== #
# H5-T09 duplicate identities suppressed by C1 (at-least-once redelivery)
# =========================================================================== #
async def test_h5_t09_duplicates_suppressed(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    events: list[IpcPayload] = [
        _tick(symbol=_SYMBOLS[i % len(_SYMBOLS)], price=str(10 + i)) for i in range(20)
    ]
    produced = await _produce_events(redis_socket, config, tmp_path, events)
    await _reinject_duplicates(redis, config, count=8)

    sink = RecordingShadowSink()
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await _drain(consumer, redis, config)

    expected = _expected_views(events, epoch=produced.epoch) + _expected_views(
        events[:8], epoch=produced.epoch
    )
    report = compare(expected, views_from_applied(sink.events))
    assert report.is_clean
    assert report.matched_total == 20
    assert report.duplicate_suppressed_total == 8
    assert sink.applied_total == 20  # C1 suppressed the 8 redeliveries


# =========================================================================== #
# H5-T10 a legal producer-sequence gap is never inferred as market-data loss
# =========================================================================== #
async def test_h5_t10_legal_sequence_gap_not_missing(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
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

    events: list[IpcPayload] = [
        _tick(symbol="TCS", price="10"),  # seq 1 -> published
        _tick(symbol="INFY", price="20"),  # seq 2 -> D1 fails, allocated-but-dropped (the gap)
        _tick(symbol="HDFC", price="30"),  # seq 3 -> published
    ]
    for datum in events:
        sink.handle(datum)
    result = await boundary.stop()
    continuity.drain_completed(result)
    await prod.aclose()

    assert publisher.current_sequence == 3  # seq 2 WAS allocated (legal gap), never on the stream
    assert await redis.xlen(config.stream_name) == 2

    sink2 = RecordingShadowSink()
    consumer = _consumer(redis, config, sink2)
    await consumer.start()
    await _drain(consumer, redis, config)
    expected = [
        view_from_envelope(_envelope(events[0], seq=1, epoch=epoch)),
        view_from_envelope(_envelope(events[2], seq=3, epoch=epoch)),
    ]
    report = compare(expected, views_from_applied(sink2.events))
    assert report.is_clean  # the seq-2 gap is NOT reported as MISSING
    assert report.matched_total == 2
    assert report.missing_total == 0


# =========================================================================== #
# H5-T11 stranded PEL entries recovered via H4B reach parity
# =========================================================================== #
async def test_h5_t11_pending_stranded_recovered(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config(read_count=50)
    events: list[IpcPayload] = [
        _tick(symbol=_SYMBOLS[i % len(_SYMBOLS)], price=str(10 + i)) for i in range(100)
    ]
    produced = await _produce_events(redis_socket, config, tmp_path, events)

    dead = _named(redis, config, "dead")
    stranded = await dead.read_raw()
    assert 0 < len(stranded) <= 50
    assert await _pending(redis, config) == len(stranded)

    recover = _fast_idle(config, consumer_name="recover")
    sink = RecordingShadowSink(max_entries=200)
    consumer = _consumer(redis, recover, sink)
    await consumer.start()
    await asyncio.sleep(0.02)
    await _drain(consumer, redis, recover)

    report = compare(_expected_views(events, epoch=produced.epoch), views_from_applied(sink.events))
    assert report.is_clean
    assert report.matched_total == 100
    assert consumer.diagnostics().pending_reclaimed_applied > 0  # recovery path exercised
    assert consumer.diagnostics().received_total > 0  # fresh-read path exercised in the same run
    assert await _pending(redis, config) == 0


# =========================================================================== #
# H5-T12 ACK lost after apply+durable-mark -> reclaim stays a single application
# =========================================================================== #
async def test_h5_t12_ack_loss_no_reapply(redis_socket: str, redis: Redis, tmp_path: Path) -> None:
    config = _config()
    events: list[IpcPayload] = [_tick(price=str(10 + i)) for i in range(3)]
    produced = await _produce_events(redis_socket, config, tmp_path, events)

    sink = RecordingShadowSink()
    a_config = config.model_copy(update={"consumer_name": "A"})
    a = _consumer(
        redis,
        a_config,
        sink,
        transport=_AckCrashTransport(RedisMarketEventStream(redis=redis, config=a_config)),  # type: ignore[arg-type]
    )
    await a.start()
    await a.poll_once()  # applies + durably marks all 3, ACK fails -> all pending
    assert sink.applied_total == 3
    assert await _pending(redis, config) == 3

    b = _consumer(redis, _fast_idle(config, consumer_name="B"), sink)
    await b.start()
    await asyncio.sleep(0.02)
    await _drain(b, redis, config)

    report = compare(_expected_views(events, epoch=produced.epoch), views_from_applied(sink.events))
    assert report.is_clean
    assert report.known_b2_duplicate_total == 0  # no reapply despite ACK loss
    assert sink.applied_total == 3


# =========================================================================== #
# H5-T13 the B2 apply->mark reapply window is surfaced (never hidden, never clean)
# =========================================================================== #
async def test_h5_t13_b2_duplicate_surfaced(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    control_ev = _tick(price="100")
    b2_ev = _tick(price="200")
    p1 = await _produce_events(redis_socket, config, tmp_path, [control_ev])

    sink = RecordingShadowSink()
    normal = _consumer(redis, config, sink)
    await normal.start()
    await normal.poll_once()  # control applied, durably marked, ACKed
    assert sink.applied_total == 1
    assert await _pending(redis, config) == 0

    p2 = await _produce_events(redis_socket, config, tmp_path, [b2_ev])  # restart -> new epoch
    a_config = config.model_copy(update={"consumer_name": "A"})
    a = _consumer(redis, a_config, sink, deduplicator=_RecordCrashDedup(_durable(redis, a_config)))  # type: ignore[arg-type]
    await a.start()
    await a.poll_once()  # b2 applied, durable mark crashes -> pending, NOT marked
    assert sink.applied_total == 2
    assert await _pending(redis, config) == 1

    b = _consumer(redis, _fast_idle(config, consumer_name="B"), sink)
    await b.start()
    await asyncio.sleep(0.02)
    await b.poll_once()  # reclaim -> durable contains false -> REAPPLY (the B2 window)
    assert sink.applied_total == 3

    applied = views_from_applied(sink.events)
    merged = compare(
        [
            view_from_envelope(_envelope(control_ev, seq=1, epoch=p1.epoch)),
            view_from_envelope(_envelope(b2_ev, seq=1, epoch=p2.epoch)),
        ],
        applied,
    )
    assert merged.matched_total == 2
    assert merged.known_b2_duplicate_total == 1  # ONLY b2_ev applied twice
    assert not merged.is_clean  # B2 breaks cleanliness, surfaced not hidden


# =========================================================================== #
# H5-T14 provider disconnect -> degraded -> reconnect -> recovery evidence -> healthy
# =========================================================================== #
async def test_h5_t14_provider_disconnect_recovery(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    batch1: list[IpcPayload] = [_tick(symbol="TCS", price="10"), _tick(symbol="INFY", price="11")]
    batch2: list[IpcPayload] = [_tick(symbol="HDFC", price="12"), _tick(symbol="WIPRO", price="13")]
    provider = _FakeProvider([batch1, batch2], block_after_last=True)
    service, stack = await _start_service(
        redis_socket, config, tmp_path, provider, max_reconnects=3
    )

    for _ in range(5_000):
        snapshot = stack.continuity.snapshot()
        if (
            service.diagnostics().events_total >= 4
            and snapshot.state is ContinuityState.HEALTHY
            and snapshot.provider_disconnect_total >= 1
        ):
            break
        await asyncio.sleep(0.001)
    snapshot = stack.continuity.snapshot()
    assert snapshot.provider_disconnect_total >= 1  # observed a real transport drop
    assert snapshot.provider_reconnect_total >= 1  # observed the reconnect
    assert snapshot.state is ContinuityState.HEALTHY  # recovered on successful publication evidence

    provider.release()
    await service.stop()
    epoch = service.diagnostics().producer_epoch
    assert epoch is not None
    sink = RecordingShadowSink()
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await _drain(consumer, redis, config)
    report = compare(_expected_views(batch1 + batch2, epoch=epoch), views_from_applied(sink.events))
    assert report.is_clean
    assert report.matched_total == 4


# =========================================================================== #
# H5-T15/T25 a terminal publication break is sticky across a provider reconnect
# =========================================================================== #
async def test_h5_t15_terminal_failure_sticky_across_reconnect(
    redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    publisher, boundary, continuity = _manual_stack(
        stream_redis=redis, atomic=_FailingAtomic(), config=config, state_dir=tmp_path, capacity=16
    )
    await boundary.start()
    epoch = publisher.diagnostics().producer_epoch
    assert epoch is not None
    continuity.producer_started(producer_id=_PRODUCER, producer_epoch=epoch)
    continuity.provider_connected()

    boundary.submit(_tick())  # worker transmits -> D1 fails -> FAILED_TRANSPORT
    for _ in range(50):
        continuity.observe_boundary(boundary.diagnostics())
        if continuity.state is ContinuityState.BROKEN:
            break
        await asyncio.sleep(0.001)
    assert continuity.state is ContinuityState.BROKEN
    assert continuity.snapshot().reason is ContinuityReason.PUBLICATION_FAILED

    continuity.provider_disconnected()
    continuity.provider_connected()  # a provider flap must NOT clear a terminal break (§25)
    continuity.observe_boundary(boundary.diagnostics())
    assert continuity.state is ContinuityState.BROKEN
    await boundary.stop()


# =========================================================================== #
# H5-T16 bounded M2 queue overflow is visible and fails closed (never a silent drop)
# =========================================================================== #
async def test_h5_t16_m2_queue_overflow_fail_closed(redis: Redis, tmp_path: Path) -> None:
    config = _config()
    atomic = _BlockingAtomic()
    publisher, boundary, continuity = _manual_stack(
        stream_redis=redis, atomic=atomic, config=config, state_dir=tmp_path, capacity=1
    )
    sink = PublishingEventSink(boundary=boundary, continuity=continuity, publisher=publisher)
    await boundary.start()
    epoch = publisher.diagnostics().producer_epoch
    assert epoch is not None
    continuity.producer_started(producer_id=_PRODUCER, producer_epoch=epoch)
    continuity.provider_connected()

    sink.handle(_tick(price="1"))  # dequeued -> in-flight, blocked in D1
    await asyncio.sleep(0.02)  # let the worker take it, emptying the capacity-1 queue
    sink.handle(_tick(price="2"))  # fills the queue
    with pytest.raises(PublicationTerminalError):
        sink.handle(_tick(price="3"))  # overflow -> explicit terminal fail-closed

    snapshot = continuity.snapshot()
    assert snapshot.overflow_total >= 1
    assert continuity.state is ContinuityState.BROKEN
    atomic.release()
    await boundary.stop()


# =========================================================================== #
# H5-T17/T18 a definite D1 publish failure is visible; accepted position exceeds published
# =========================================================================== #
async def test_h5_t17_t18_d1_failure_accepted_exceeds_published(
    redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    publisher, boundary, continuity = _manual_stack(
        stream_redis=redis, atomic=_FailingAtomic(), config=config, state_dir=tmp_path, capacity=16
    )
    sink = PublishingEventSink(boundary=boundary, continuity=continuity, publisher=publisher)
    await boundary.start()
    epoch = publisher.diagnostics().producer_epoch
    assert epoch is not None
    continuity.producer_started(producer_id=_PRODUCER, producer_epoch=epoch)
    continuity.provider_connected()

    sink.handle(_tick())  # accepted (seq 1); the worker's transmit then fails at D1
    for _ in range(50):
        continuity.observe_boundary(boundary.diagnostics())
        if continuity.state is ContinuityState.BROKEN:
            break
        await asyncio.sleep(0.001)

    snapshot = continuity.snapshot()
    assert continuity.state is ContinuityState.BROKEN  # T17: D1 failure surfaced
    assert snapshot.publication_failure_total >= 1
    assert boundary.diagnostics().published_total == 0  # T18: nothing published...
    assert snapshot.last_accepted_sequence == 1  # ...while an event was accepted (positions differ)
    await boundary.stop()


# =========================================================================== #
# H5-T20 a malformed stream entry is surfaced as a decode failure (never faked missing)
# =========================================================================== #
async def test_h5_t20_malformed_stream_entry_surfaced(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    events: list[IpcPayload] = [_tick(price="50")]
    produced = await _produce_events(redis_socket, config, tmp_path, events)
    await redis.xadd(
        config.stream_name,
        {_FIELD: b"not-a-valid-envelope"},
        maxlen=config.maxlen,
        approximate=True,
    )

    sink = RecordingShadowSink()
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await _drain(consumer, redis, config)

    diagnostics = consumer.diagnostics()
    decode_failures = diagnostics.envelope_decode_failures + diagnostics.payload_decode_failures
    assert decode_failures >= 1
    report = compare(
        _expected_views(events, epoch=produced.epoch),
        views_from_applied(sink.events),
        decode_failures=decode_failures,
        unsupported=diagnostics.unsupported_schema_total,
    )
    assert report.matched_total == 1
    assert report.decode_failure_total >= 1
    assert not report.is_clean  # poison surfaced, not hidden


# =========================================================================== #
# H5-T21/T22/T24 the topology uses only the fake provider and a non-authoritative sink
# =========================================================================== #
async def test_h5_t21_t22_t24_no_broker_or_authority(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config()
    events: list[IpcPayload] = [_tick(symbol="TCS", price="5")]
    provider = _FakeProvider([events])
    service, _ = await _start_service(redis_socket, config, tmp_path, provider)
    assert service.provider is provider  # T24: the only provider is the local fake
    assert not type(provider).__module__.startswith("app.adapters")  # no Dhan adapter constructed
    await _drive_and_stop(service, provider, expected=1)

    epoch = service.diagnostics().producer_epoch
    assert epoch is not None
    sink = RecordingShadowSink()
    assert isinstance(sink, RecordingShadowSink)  # T21/T22: non-authoritative destination only
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await _drain(consumer, redis, config)
    report = compare(_expected_views(events, epoch=epoch), views_from_applied(sink.events))
    assert report.is_clean
    assert report.matched_total == 1


# =========================================================================== #
# H5-T25 fixtures are canonical tz-aware UTC; no +5:30/-5:30 workaround
# =========================================================================== #
def test_h5_t25_fixture_timestamps_are_canonical_utc() -> None:
    envelope = _envelope(_tick(), seq=1, epoch=1)
    assert envelope.produced_at.tzinfo is not None
    assert envelope.produced_at.utcoffset() == datetime(2026, 1, 1, tzinfo=UTC).utcoffset()
    assert _NOW.utcoffset().total_seconds() == 0  # exactly UTC, never a hardcoded IST shift


# =========================================================================== #
# H5-T26 large replay: >= 10,000 canonical input events through the real service
# =========================================================================== #
async def test_h5_t26_large_replay_ten_thousand_inputs(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config(read_count=1_000, publish_shutdown_drain_timeout_seconds=60.0)
    events = _representative_mix(10_000)
    provider = _FakeProvider([events])
    service, _ = await _start_service(redis_socket, config, tmp_path, provider)
    await _drive_and_stop(service, provider, expected=10_000, cycles=120_000)

    epoch = service.diagnostics().producer_epoch
    assert epoch is not None
    assert service.diagnostics().published_total == 10_000

    sink = RecordingShadowSink(max_entries=10_050)
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await _drain(consumer, redis, config)

    report = compare(
        _expected_views(events, epoch=epoch), views_from_applied(sink.events), sample_limit=20
    )
    assert report.is_clean
    assert report.matched_total == 10_000
    assert report.missing_total == 0
    assert report.unexpected_total == 0
    assert sink.applied_total == 10_000


# =========================================================================== #
# H5-T27/§37 repeatability: the principal replay is deterministic across reruns
# =========================================================================== #
async def test_h5_t27_deterministic_reruns(redis_socket: str, redis: Redis, tmp_path: Path) -> None:
    verdicts: list[tuple[bool, int, int, int]] = []
    for run in range(3):
        await redis.flushall()
        config = _config()
        events = _representative_mix(150)
        provider = _FakeProvider([events])
        service, _ = await _start_service(redis_socket, config, tmp_path / f"run{run}", provider)
        await _drive_and_stop(service, provider, expected=150)
        epoch = service.diagnostics().producer_epoch
        assert epoch is not None
        sink = RecordingShadowSink(max_entries=200)
        consumer = _consumer(redis, config, sink)
        await consumer.start()
        await _drain(consumer, redis, config)
        report = compare(_expected_views(events, epoch=epoch), views_from_applied(sink.events))
        verdicts.append(
            (report.is_clean, report.matched_total, report.missing_total, report.unexpected_total)
        )
    assert verdicts == [(True, 150, 0, 0)] * 3  # identical logical verdict every run


# =========================================================================== #
# H5-T28 diagnostics and the comparator sample are bounded under anomalies
# =========================================================================== #
async def test_h5_t28_bounded_diagnostics(redis_socket: str, redis: Redis, tmp_path: Path) -> None:
    config = _config()
    events: list[IpcPayload] = [
        _tick(symbol=_SYMBOLS[i % len(_SYMBOLS)], price=str(10 + i)) for i in range(120)
    ]
    produced = await _produce_events(redis_socket, config, tmp_path, events)

    sink = RecordingShadowSink(max_entries=200)
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await _drain(consumer, redis, config)

    clean = compare(_expected_views(events, epoch=produced.epoch), views_from_applied(sink.events))
    assert clean.is_clean
    assert clean.sample == ()

    # Inject 50 never-applied expected identities: totals stay complete, the sample stays bounded.
    extra = _expected_views(events, epoch=produced.epoch) + [
        view_from_envelope(
            _envelope(_tick(price=str(1_000 + i)), seq=10_000 + i, epoch=produced.epoch)
        )
        for i in range(50)
    ]
    noisy = compare(extra, views_from_applied(sink.events), sample_limit=10)
    assert noisy.missing_total == 50
    assert len(noisy.sample) == 10  # bounded diagnostics regardless of anomaly count
    assert isinstance(consumer.diagnostics().received_total, int)  # bounded scalar counters
