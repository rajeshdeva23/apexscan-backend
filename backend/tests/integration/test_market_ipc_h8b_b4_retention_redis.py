"""B4 closure end-to-end over real Redis — dedup retention vs. redelivery horizon (PHASE H8B).

The config invariant proofs live in ``tests/unit/test_market_ipc_retention_invariant.py``. This
module proves the mechanism against a disposable ``redislite`` server (never a shared/production
Redis): the D1 producer trims the stream by AGE to ``max_redelivery_horizon_seconds`` (so an event
stops being redeliverable once older), the durable dedup key outlives that horizon, and a pending
entry that has been trimmed is cleanly ACKed away rather than re-applied.

Covers H8B-T05/T07/T08/T10/T14/T15/T17/T18. Uses real XADD/XTRIM(MINID)/XREADGROUP/XPENDING/
XAUTOCLAIM/XACK/TTL. No TickEngine/MarketContext authority, no Dhan, no production composition.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis

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
)
from app.market_ipc.atomic import RedisAtomicPublisher
from app.market_ipc.durable_dedup import dedup_key
from app.market_ipc.envelope import ProducerEventIdentity
from app.market_ipc.events import IpcPayload
from app.schemas.market_data import Instrument, Tick

redislite = pytest.importorskip("redislite", reason="disposable real Redis unavailable")

_NOW = datetime(2026, 9, 14, 6, 30, tzinfo=UTC)
_TD = date(2026, 9, 14)
_PRODUCER = "market-ingestion"


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


def _config(**overrides: object) -> MarketIpcConfig:
    return MarketIpcConfig(block_ms=0, **overrides)


def _fast_idle(config: MarketIpcConfig) -> MarketIpcConfig:
    # model_copy bypasses the >=1000ms field bound for a deterministic reclaim in tests (as H4B).
    return config.model_copy(update={"claim_idle_ms": 1})


def _tick(symbol: str = "TCS", *, seq: int) -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        event_timestamp=_NOW,
        last_price=Decimal(str(100 + seq)),
    )


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


def _durable(redis: Redis, config: MarketIpcConfig) -> CompositeDeduplicator:
    return CompositeDeduplicator(
        memory=BoundedDeduplicator(config.dedup_max_entries),
        durable=DurableDeduplicator(redis, config),
    )


async def _publish(publisher: RedisAtomicPublisher, *, start: int, count: int) -> list[str]:
    ids: list[str] = []
    for i in range(start, start + count):
        result = await publisher.publish_stream_only(_envelope(_tick(seq=i), seq=i))
        ids.append(result.message_id)
    return ids


# =========================================================================== #
# T05/T18: the producer age-trims the stream to the horizon (redelivery becomes impossible)
# =========================================================================== #
async def test_h8b_t05_t18_stream_is_age_trimmed_to_horizon(redis: Redis) -> None:
    config = _config(
        max_redelivery_horizon_seconds=2, retention_safety_margin_seconds=0, dedup_ttl_seconds=3_600
    )
    publisher = RedisAtomicPublisher(redis, config)

    old_ids = await _publish(publisher, start=1, count=200)  # fills whole macro nodes
    await asyncio.sleep(2.5)  # exceed the 2s horizon
    await _publish(publisher, start=1_000, count=200)  # each publish age-trims older nodes

    length = await redis.xlen(config.stream_name)
    assert length < 400  # old, fully-aged macro nodes were evicted (not merely MAXLEN-capped)
    oldest = await redis.xrange(config.stream_name, min=old_ids[0], max=old_ids[0])
    assert (
        oldest == []
    )  # the first (oldest) entry is past the horizon and gone -> not redeliverable


# =========================================================================== #
# T05/T07: the durable dedup key outlives the redelivery horizon (the invariant, realized)
# =========================================================================== #
async def test_h8b_t05_dedup_key_ttl_exceeds_redelivery_horizon(redis: Redis) -> None:
    config = _config(
        max_redelivery_horizon_seconds=3_600,
        retention_safety_margin_seconds=0,
        dedup_ttl_seconds=7_200,
    )
    identity = ProducerEventIdentity(_PRODUCER, 1, 1)
    await DurableDeduplicator(redis, config).record(identity)
    ttl = await redis.ttl(dedup_key(config.dedup_key_prefix, identity))
    assert ttl > config.max_redelivery_horizon_seconds  # key survives past every legitimate reclaim


# =========================================================================== #
# T14: a pending entry that has been trimmed is ACKed away, never re-applied
# =========================================================================== #
async def test_h8b_t14_trimmed_pending_entry_is_not_reapplied(redis: Redis) -> None:
    config = _fast_idle(
        _config(
            max_redelivery_horizon_seconds=2,
            retention_safety_margin_seconds=0,
            dedup_ttl_seconds=3_600,
        )
    )
    publisher = RedisAtomicPublisher(redis, config)
    [victim_id] = await _publish(publisher, start=1, count=1)

    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    delivered = await stream.read_raw()  # deliver the victim into this consumer's PEL (unacked)
    assert any(mid == victim_id for mid, _ in delivered)
    assert await redis.xlen(config.stream_name) == 1

    # Deterministically evict the still-pending entry from the stream (models an age-trim past it).
    await redis.xtrim(config.stream_name, minid=_after(victim_id), approximate=False)
    assert await redis.xrange(config.stream_name, min=victim_id, max=victim_id) == []

    sink = RecordingShadowSink()
    consumer = MarketEventConsumer(
        transport=RedisMarketEventStream(redis=redis, config=config),
        config=config,
        sink=sink,
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: 7,
        now=lambda: _NOW,
        deduplicator=_durable(redis, config),
    )
    await consumer.start()
    await asyncio.sleep(0.02)
    # Reclaiming a trimmed pending entry must not crash the loop (its XAUTOCLAIM tombstone is
    # skipped) and must never re-apply it. Poll twice: a second cycle proves the tombstone is
    # handled idempotently rather than jamming the consumer.
    await consumer.poll_once()
    await consumer.poll_once()

    assert sink.applied_total == 0  # the trimmed entry is gone from the stream and never re-applied
    assert consumer.diagnostics().read_failures == 0  # reclaim did not error the cycle


# =========================================================================== #
# T07/T08: an applied+marked event whose ACK is lost is not re-applied on reclaim within the horizon
# =========================================================================== #
async def test_h8b_t07_t08_applied_dedup_suppresses_reclaim(redis: Redis) -> None:
    config = _config(
        max_redelivery_horizon_seconds=2,
        retention_safety_margin_seconds=0,
        dedup_ttl_seconds=3_600,
    )
    identity = ProducerEventIdentity(_PRODUCER, 1, 5)
    durable = _durable(redis, config)
    await durable.record(identity)  # models "applied + durably marked" before the ACK was lost
    # A fresh process (empty memory cache) still sees the durable mark within the horizon.
    fresh = _durable(redis, config)
    assert await fresh.contains(identity) is True


# =========================================================================== #
# T10: same sequence under different producer epochs are distinct dedup identities
# =========================================================================== #
async def test_h8b_t10_same_seq_new_epoch_distinct_dedup(redis: Redis) -> None:
    config = _config()
    durable = DurableDeduplicator(redis, config)
    epoch1 = ProducerEventIdentity(_PRODUCER, 1, 9)
    epoch2 = ProducerEventIdentity(_PRODUCER, 2, 9)  # same seq, newer epoch
    await durable.record(epoch1)
    assert await durable.contains(epoch1) is True
    assert await durable.contains(epoch2) is False  # not suppressed by the other epoch's key
    assert dedup_key(config.dedup_key_prefix, epoch1) != dedup_key(config.dedup_key_prefix, epoch2)


# =========================================================================== #
# T17: durable dedup state survives a fresh Redis client (no reliance on process-local cache)
# =========================================================================== #
async def test_h8b_t17_dedup_survives_new_client(redis_socket: str) -> None:
    config = _config()
    identity = ProducerEventIdentity(_PRODUCER, 3, 3)
    writer: Redis = Redis(unix_socket_path=redis_socket)
    await writer.flushall()
    await DurableDeduplicator(writer, config).record(identity)
    await writer.aclose()

    reader: Redis = Redis(unix_socket_path=redis_socket)
    try:
        assert await DurableDeduplicator(reader, config).contains(identity) is True
    finally:
        await reader.aclose()


def _after(message_id: str) -> str:
    """The stream id immediately after ``message_id`` (so a MINID trim evicts it exactly)."""
    ms, seq = message_id.split("-")
    return f"{ms}-{int(seq) + 1}"
