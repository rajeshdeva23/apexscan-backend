"""Real disposable-Redis replay + parity integration for H4C (DECOUPLING PHASE H4C).

Runs a real ``redis-server`` (bundled by ``redislite`` on a private unix socket) — never a
shared or production Redis. Publishes canonical fixture events through the Phase-A/B stream,
consumes them through the H4A/H4B shadow consumer with durable C1 dedup, captures the
non-authoritative applied output, and compares it against the expected fixtures with the H4C
comparator. Proves: clean multi-kind parity; pending-recovery parity; ACK-lost single logical
application; the B2 apply->mark window surfaced (not hidden); malformed-entry decode failures
surfaced; reference-event comparison; and a bounded >=5000-event replay with multiple
instruments, kinds, epochs, legal gaps, and duplicates. Skips cleanly if redislite is absent.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.market_ipc import (
    BoundedDeduplicator,
    CompositeDeduplicator,
    DurableDeduplicator,
    EventKind,
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
from app.market_ipc.envelope import ProducerEventIdentity
from app.market_ipc.events import IpcPayload
from app.schemas.market_data import (
    FeedContinuity,
    FeedContinuityEvent,
    Instrument,
    MarketReference,
    ProviderSessionOhlc,
    Quote,
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


def _reference(symbol: str = "TCS", previous_close: str = "99.25") -> MarketReference:
    return MarketReference(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        previous_close=Decimal(previous_close),
    )


def _continuity() -> FeedContinuityEvent:
    return FeedContinuityEvent(status=FeedContinuity.CONNECTED, observed_at=_NOW)


def _envelope(payload: IpcPayload, *, seq: int, epoch: int = 1) -> MarketEventEnvelope:
    return build_envelope(
        payload,
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=7,
    )


def _replay_fixture(unique_count: int) -> list[MarketEventEnvelope]:
    """Deterministic envelopes across instruments, kinds, epochs, and legal +2 sequence gaps."""
    envelopes: list[MarketEventEnvelope] = []
    seq = 0
    for i in range(unique_count):
        seq += 2  # legal +2 gap: identity is globally unique via seq, never reused
        symbol = _SYMBOLS[i % len(_SYMBOLS)]
        selector = i % 4
        if selector == 0:
            payload: IpcPayload = _tick(symbol=symbol, price=str(100 + (i % 50)))
        elif selector == 1:
            payload = _quote(symbol=symbol)
        elif selector == 2:
            payload = _reference(symbol=symbol)
        else:
            payload = _tick_with_ohlc(symbol=symbol)
        envelopes.append(_envelope(payload, seq=seq, epoch=1 + (i % 3)))
    return envelopes


# --------------------------------------------------------------------------- #
# Consumer wiring (durable C1) — mirrors the H4A/H4B integration helpers
# --------------------------------------------------------------------------- #
def _config(**overrides: object) -> MarketIpcConfig:
    return MarketIpcConfig(block_ms=0, **overrides)


def _named(redis: Redis, config: MarketIpcConfig, name: str) -> RedisMarketEventStream:
    return RedisMarketEventStream(
        redis=redis, config=config.model_copy(update={"consumer_name": name})
    )


def _fast_idle(config: MarketIpcConfig, *, consumer_name: str) -> MarketIpcConfig:
    # model_copy bypasses the ge=1000 floor for a fast test idle window (matches H4A/H4B tests).
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
    # A poll that ACKs nothing new AND leaves nothing pending means the stream is exhausted for
    # this consumer (XPENDING alone is 0 whenever the last batch was ACKed, even with undelivered
    # entries still on the stream), so both conditions are needed to detect true completion.
    for _ in range(200):
        before = consumer.diagnostics().acked_total
        await consumer.poll_once()
        if consumer.diagnostics().acked_total == before and await _pending(redis, config) == 0:
            return
    raise AssertionError("stream did not drain within the cycle budget")


# --------------------------------------------------------------------------- #
# Test doubles (reused from the H4B recovery suite): inject one failure mode.
# --------------------------------------------------------------------------- #
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
# T01 / T12 / T24: a clean multi-kind replay reaches full canonical parity
# =========================================================================== #
async def test_multi_kind_replay_reaches_parity(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    payloads: list[IpcPayload] = [_tick(), _tick_with_ohlc(), _quote(), _reference(), _continuity()]
    envelopes = [_envelope(payload, seq=i) for i, payload in enumerate(payloads, start=1)]
    for envelope in envelopes:
        await producer.publish(envelope)

    sink = RecordingShadowSink()
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await _drain(consumer, redis, config)

    report = compare([view_from_envelope(e) for e in envelopes], views_from_applied(sink.events))
    assert report.is_clean
    assert report.matched_total == 5
    kinds = {view.kind for view in views_from_applied(sink.events)}
    assert kinds == {
        EventKind.TICK,
        EventKind.QUOTE,
        EventKind.MARKET_REFERENCE,
        EventKind.FEED_CONTINUITY,
    }


async def test_reference_event_value_mismatch_is_detected(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(_reference(previous_close="99.25"), seq=1))

    sink = RecordingShadowSink()
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await _drain(consumer, redis, config)

    expected_wrong = _envelope(_reference(previous_close="88.00"), seq=1)  # same identity, drift
    report = compare([view_from_envelope(expected_wrong)], views_from_applied(sink.events))
    assert report.value_mismatch_total == 1
    assert report.sample[0].field == "previous_close"
    assert report.sample[0].expected == "88.00"
    assert report.sample[0].actual == "99.25"


# =========================================================================== #
# T05: fixture duplicates are suppressed by C1 and read as healthy parity
# =========================================================================== #
async def test_duplicate_replay_is_suppressed_not_missing(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    e1, e2, e3 = (
        _envelope(_tick(price="10"), seq=1),
        _envelope(_tick(price="20"), seq=2),
        _envelope(_tick(price="30"), seq=3),
    )
    for envelope in (e1, e2, e1, e3):  # E1 published twice (same identity)
        await producer.publish(envelope)

    sink = RecordingShadowSink()
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await _drain(consumer, redis, config)

    report = compare(
        [view_from_envelope(e) for e in (e1, e2, e1, e3)], views_from_applied(sink.events)
    )
    assert report.is_clean
    assert report.matched_total == 3
    assert report.duplicate_suppressed_total == 1
    assert sink.applied_total == 3


# =========================================================================== #
# T08: entries stranded in a dead PEL are recovered and reach parity
# =========================================================================== #
async def test_pending_recovery_reaches_parity(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    envelopes = [_envelope(_tick(price=str(100 + i)), seq=i) for i in range(1, 21)]
    for envelope in envelopes:
        await producer.publish(envelope)
    dead = _named(redis, config, "dead")
    while await dead.read_raw():  # strand all 20 in the dead consumer's PEL
        pass
    assert await _pending(redis, config) == 20

    sink = RecordingShadowSink(max_entries=100)
    consumer = _consumer(redis, _fast_idle(config, consumer_name="recover"), sink)
    await consumer.start()
    await asyncio.sleep(0.02)  # exceed the 1ms idle threshold
    await _drain(consumer, redis, config)

    report = compare([view_from_envelope(e) for e in envelopes], views_from_applied(sink.events))
    assert report.is_clean
    assert report.matched_total == 20


# =========================================================================== #
# T09: an ACK-lost entry, once reclaimed, is a single logical application
# =========================================================================== #
async def test_ack_lost_recovery_is_single_logical_application(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    envelope = _envelope(_tick(), seq=1)
    await producer.publish(envelope)

    sink = RecordingShadowSink()  # shared across A and B — a reapply would show as B2
    a_config = config.model_copy(update={"consumer_name": "A"})
    a = _consumer(
        redis,
        a_config,
        sink,
        transport=_AckCrashTransport(RedisMarketEventStream(redis=redis, config=a_config)),  # type: ignore[arg-type]
    )
    await a.start()
    await a.poll_once()  # applied + durably marked, ACK failed -> pending
    assert await _pending(redis, config) == 1

    b = _consumer(redis, _fast_idle(config, consumer_name="B"), sink)
    await b.start()
    await asyncio.sleep(0.02)
    await b.poll_once()  # reclaim -> durable duplicate -> no reapply

    report = compare([view_from_envelope(envelope)], views_from_applied(sink.events))
    assert report.is_clean
    assert report.matched_total == 1
    assert report.known_b2_duplicate_total == 0
    assert await _pending(redis, config) == 0


# =========================================================================== #
# T10: the apply->mark (B2) reapply window is surfaced by the comparator
# =========================================================================== #
async def test_b2_window_is_surfaced_by_comparator(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    envelope = _envelope(_tick(), seq=1)
    await producer.publish(envelope)

    sink = RecordingShadowSink()
    a_config = config.model_copy(update={"consumer_name": "A"})
    a = _consumer(redis, a_config, sink, deduplicator=_RecordCrashDedup(_durable(redis, a_config)))  # type: ignore[arg-type]
    await a.start()
    await a.poll_once()  # apply ok, durable mark crashes -> pending, not marked
    assert sink.applied_total == 1

    b = _consumer(redis, _fast_idle(config, consumer_name="B"), sink)
    await b.start()
    await asyncio.sleep(0.02)
    await b.poll_once()  # reclaim -> durable contains false -> reapply (the B2 window)

    report = compare([view_from_envelope(envelope)], views_from_applied(sink.events))
    assert report.known_b2_duplicate_total == 1  # surfaced, never hidden or "solved"
    assert report.matched_total == 1
    assert not report.is_clean
    assert await _pending(redis, config) == 0


# =========================================================================== #
# T11: a malformed stream entry surfaces as a decode failure, not silent loss
# =========================================================================== #
async def test_malformed_entry_surfaces_decode_failure(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    good = [_envelope(_tick(price=str(100 + i)), seq=i) for i in range(1, 4)]
    for envelope in good:
        await producer.publish(envelope)
    await redis.xadd(config.stream_name, {"e": b"not-a-valid-envelope"})  # permanent poison

    sink = RecordingShadowSink()
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await _drain(consumer, redis, config)

    diagnostics = consumer.diagnostics()
    report = compare(
        [view_from_envelope(e) for e in good],
        views_from_applied(sink.events),
        decode_failures=diagnostics.envelope_decode_failures,
    )
    assert report.matched_total == 3
    assert report.decode_failure_total == 1  # poison surfaced, not silently dropped
    assert report.missing_total == 0
    assert not report.is_clean
    assert await _pending(redis, config) == 0  # poison terminally ACKed


# =========================================================================== #
# T15 / T46: bounded >=5000-event replay — instruments, kinds, epochs, gaps, dups
# =========================================================================== #
async def test_large_replay_reaches_parity(redis: Redis) -> None:
    base = _config(read_count=500)
    producer = RedisMarketEventStream(redis=redis, config=base)
    await producer.ensure_group()
    unique = _replay_fixture(5_000)
    for envelope in unique:
        await producer.publish(envelope)
    duplicates = unique[:500]  # re-publish 500 identities -> must be suppressed by C1
    for envelope in duplicates:
        await producer.publish(envelope)

    sink = RecordingShadowSink(max_entries=6_000)
    consumer = _consumer(redis, base, sink)
    await consumer.start()
    await _drain(consumer, redis, base)

    expected = [view_from_envelope(e) for e in (*unique, *duplicates)]
    report = compare(expected, views_from_applied(sink.events), sample_limit=20)
    assert report.matched_total == 5_000
    assert report.duplicate_suppressed_total == 500
    assert report.missing_total == 0
    assert report.unexpected_total == 0
    assert report.value_mismatch_total == 0
    assert report.known_b2_duplicate_total == 0
    assert report.is_clean
    assert report.sample == ()  # a clean replay retains no mismatch sample
    assert sink.applied_total == 5_000  # each unique identity applied exactly once


# =========================================================================== #
# Determinism: the same replay produces the same parity report (no wall-clock)
# =========================================================================== #
async def test_replay_is_deterministic(redis: Redis) -> None:
    async def _run() -> tuple[int, int]:
        await redis.flushall()
        config = _config()
        producer = RedisMarketEventStream(redis=redis, config=config)
        await producer.ensure_group()
        envelopes = _replay_fixture(50)
        for envelope in envelopes:
            await producer.publish(envelope)
        sink = RecordingShadowSink(max_entries=100)
        consumer = _consumer(redis, config, sink)
        await consumer.start()
        await _drain(consumer, redis, config)
        report = compare(
            [view_from_envelope(e) for e in envelopes], views_from_applied(sink.events)
        )
        return report.matched_total, report.expected_total

    assert await _run() == await _run()
