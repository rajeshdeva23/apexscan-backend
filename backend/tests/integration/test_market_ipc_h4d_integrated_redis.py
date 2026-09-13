"""Integrated H4A+H4B+H4C offline readiness scenario (DECOUPLING PHASE H4D).

One end-to-end exercise of the full non-authoritative consumer subsystem against a real
disposable ``redis-server`` (bundled by ``redislite`` on a private unix socket) — never a shared
or production Redis. Proves the three phases compose coherently: the H4A consumer + durable C1,
H4B pending recovery / XAUTOCLAIM, and the H4C parity comparator all operate over one stream and
one durable dedup authority. Covers a large healthy replay (multiple instruments, kinds, epochs,
legal gaps, duplicates, part stranded in a dead PEL), an ACK-loss subset that stays a single
logical application, and a controlled B2 subset that is surfaced (never hidden, never clean) while
a healthy control on the same sink stays clean. Skips cleanly if redislite is absent.

H4D is a review phase: this adds evidence only. No production code changes, no live Dhan, no
production contact, no consumer activation, no IPC authority.
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


def _reference(symbol: str = "TCS") -> MarketReference:
    return MarketReference(
        instrument=Instrument(exchange="NSE", symbol=symbol), previous_close=Decimal("99.25")
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
        seq += 2  # legal +2 gap: identity stays globally unique via seq, never reused
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
# Consumer wiring (durable C1) + doubles — mirror the H4B/H4C integration helpers
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
    # A poll that ACKs nothing new AND leaves nothing pending means the stream is exhausted for
    # this consumer (XPENDING alone is 0 whenever the last batch was ACKed, even with undelivered
    # entries still on the stream), so both conditions are needed to detect true completion.
    for _ in range(200):
        before = consumer.diagnostics().acked_total
        await consumer.poll_once()
        if consumer.diagnostics().acked_total == before and await _pending(redis, config) == 0:
            return
    raise AssertionError("stream did not drain within the cycle budget")


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
# §33 healthy subset: large replay, part stranded in a dead PEL, recovered + fresh -> clean
# =========================================================================== #
async def test_integrated_healthy_scenario_reaches_clean_parity(redis: Redis) -> None:
    base = _config(read_count=1_200)
    producer = RedisMarketEventStream(redis=redis, config=base)
    await producer.ensure_group()
    unique = _replay_fixture(5_000)  # 5 instruments, 4 kinds, 3 epochs, legal +2 gaps
    for envelope in unique:
        await producer.publish(envelope)
    duplicates = unique[:500]  # re-publish 500 identities -> must be suppressed by C1
    for envelope in duplicates:
        await producer.publish(envelope)

    # Strand the first delivered page in a dead consumer's PEL; the rest stays fresh/undelivered,
    # so ONE recovering consumer must exercise BOTH the XAUTOCLAIM and the XREADGROUP paths.
    dead = _named(redis, base, "dead")
    stranded = await dead.read_raw()
    assert 0 < len(stranded) <= 1_200
    assert await _pending(redis, base) == len(stranded)

    recover = _fast_idle(base, consumer_name="recover")
    sink = RecordingShadowSink(max_entries=6_000)
    consumer = _consumer(redis, recover, sink)
    await consumer.start()
    await asyncio.sleep(0.02)  # let the stranded page exceed the 1ms idle threshold
    await _drain(consumer, redis, recover)

    diagnostics = consumer.diagnostics()
    report = compare(
        [view_from_envelope(e) for e in (*unique, *duplicates)],
        views_from_applied(sink.events),
        sample_limit=20,
    )
    assert report.is_clean
    assert report.matched_total == 5_000
    assert report.duplicate_suppressed_total == 500
    assert report.missing_total == 0
    assert report.unexpected_total == 0
    assert report.value_mismatch_total == 0
    assert report.known_b2_duplicate_total == 0
    assert sink.applied_total == 5_000
    assert diagnostics.pending_reclaimed_applied > 0  # recovery path exercised
    assert diagnostics.received_total > 0  # fresh-read path exercised in the SAME run
    assert await _pending(redis, base) == 0  # PEL fully drained


# =========================================================================== #
# §33 ACK-loss subset: applied + marked but ACK lost -> reclaim stays single application
# =========================================================================== #
async def test_integrated_ack_loss_subset_stays_single_application(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    envelopes = [_envelope(_tick(price=str(10 + i)), seq=i) for i in range(1, 4)]
    for envelope in envelopes:
        await producer.publish(envelope)

    sink = RecordingShadowSink()  # shared across A and B: a reapply would show as B2
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

    report = compare([view_from_envelope(e) for e in envelopes], views_from_applied(sink.events))
    assert report.is_clean
    assert report.matched_total == 3
    assert report.known_b2_duplicate_total == 0  # no reapply despite ACK loss
    assert sink.applied_total == 3
    assert await _pending(redis, config) == 0


# =========================================================================== #
# §33 B2 subset: surfaced (not clean) while a healthy control on the same sink stays clean
# =========================================================================== #
async def test_integrated_b2_subset_surfaced_while_control_stays_clean(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    control = _envelope(_tick(price="100"), seq=1)  # healthy: durably marked once
    b2_event = _envelope(_tick(price="200"), seq=2)  # apply->mark crash -> reclaim reapplies

    sink = RecordingShadowSink()
    await producer.publish(control)
    normal = _consumer(redis, config, sink)
    await normal.start()
    await normal.poll_once()  # control applied, durably marked, ACKed
    assert sink.applied_total == 1
    assert await _pending(redis, config) == 0

    await producer.publish(b2_event)
    a_config = config.model_copy(update={"consumer_name": "A"})
    a = _consumer(redis, a_config, sink, deduplicator=_RecordCrashDedup(_durable(redis, a_config)))  # type: ignore[arg-type]
    await a.start()
    await a.poll_once()  # b2_event applied, durable mark crashes -> pending, NOT marked
    assert sink.applied_total == 2
    assert await _pending(redis, config) == 1

    b = _consumer(redis, _fast_idle(config, consumer_name="B"), sink)
    await b.start()
    await asyncio.sleep(0.02)
    await b.poll_once()  # reclaim -> durable contains false -> REAPPLY (the B2 window)
    assert sink.applied_total == 3  # control(1) + b2_event applied twice
    assert await _pending(redis, config) == 0

    applied = views_from_applied(sink.events)
    merged = compare([view_from_envelope(control), view_from_envelope(b2_event)], applied)
    assert merged.matched_total == 2  # both identities matched at least once
    assert merged.known_b2_duplicate_total == 1  # ONLY b2_event applied twice
    assert not merged.is_clean  # B2 breaks cleanliness, surfaced not hidden

    control_id = ProducerEventIdentity.from_envelope(control)
    control_only = compare(
        [view_from_envelope(control)], [v for v in applied if v.identity == control_id]
    )
    assert control_only.is_clean  # B2 does not contaminate the healthy control's classification
    assert control_only.matched_total == 1
