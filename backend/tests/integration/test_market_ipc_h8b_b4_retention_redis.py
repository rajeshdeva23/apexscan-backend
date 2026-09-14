"""B4 closure end-to-end over real Redis — dedup retention vs. redelivery horizon (PHASE H8B).

The config invariant proofs live in ``tests/unit/test_market_ipc_retention_invariant.py``. This
module proves the mechanism against a disposable ``redislite`` server (never a shared/production
Redis): the **consumer** refuses to apply an event older than ``max_redelivery_horizon_seconds`` —
a publish-independent bound that holds even when the market is quiet and nothing trims the stream —
so a reclaimed pending entry whose dedup key has expired is dropped (lost, safe), never re-applied.
Within the horizon the fail-closed config invariant guarantees the dedup key still exists, so a
genuine redelivery is suppressed by C1.

Covers H8B-T05/T07/T08/T10/T14/T17/T18. Uses real XADD/XREADGROUP/XPENDING/XAUTOCLAIM/XACK/TTL/DEL.
No TickEngine/MarketContext authority, no Dhan, no production composition.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
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
)
from app.market_ipc.durable_dedup import dedup_key
from app.market_ipc.envelope import ProducerEventIdentity
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


def _tick(*, seq: int) -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol="TCS"),
        event_timestamp=_NOW,
        last_price=Decimal(str(100 + seq)),
    )


def _envelope(*, seq: int, epoch: int = 1) -> MarketEventEnvelope:
    return build_envelope(
        _tick(seq=seq),
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


def _consumer(
    redis: Redis,
    config: MarketIpcConfig,
    sink: RecordingShadowSink,
    *,
    now: datetime,
    transport: object | None = None,
    deduplicator: object | None = None,
) -> MarketEventConsumer:
    return MarketEventConsumer(
        transport=transport or RedisMarketEventStream(redis=redis, config=config),  # type: ignore[arg-type]
        config=config,
        sink=sink,
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: 7,
        now=lambda: now,
        deduplicator=deduplicator or _durable(redis, config),  # type: ignore[arg-type]
    )


async def _pending(redis: Redis, config: MarketIpcConfig) -> int:
    summary = await redis.xpending(config.stream_name, config.consumer_group)
    return int(summary["pending"])


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


# =========================================================================== #
# T05/T18: a pending entry reclaimed AFTER the horizon is dropped, never re-applied (HIGH-1 fix)
# =========================================================================== #
async def test_h8b_t05_t18_beyond_horizon_reclaim_is_dropped_not_reapplied(redis: Redis) -> None:
    config = _fast_idle(
        _config(
            max_redelivery_horizon_seconds=2,
            retention_safety_margin_seconds=0,
            dedup_ttl_seconds=3_600,
        )
    )
    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    envelope = _envelope(seq=1)  # produced_at = _NOW
    await stream.publish(envelope)

    sink = RecordingShadowSink()
    # Consumer A applies + records the dedup key, but its ACK is lost -> the entry stays pending.
    crash_transport = _AckCrashTransport(RedisMarketEventStream(redis=redis, config=config))
    a = _consumer(redis, config, sink, now=_NOW, transport=crash_transport)
    await a.start()
    await a.poll_once()
    assert sink.applied_total == 1
    assert await _pending(redis, config) == 1

    # Model a quiet period longer than the horizon: the dedup key TTL has elapsed (no publishes ran
    # to trim the stream, so the pending entry is still recoverable) — the exact B4 condition.
    identity = ProducerEventIdentity.from_envelope(envelope)
    await redis.delete(dedup_key(config.dedup_key_prefix, identity))

    late = _NOW + timedelta(seconds=5)  # reclaim clock is 5s past produced_at (> 2s horizon)
    b = _consumer(redis, config, sink, now=late)
    await b.start()
    await asyncio.sleep(0.02)
    await b.poll_once()  # reclaim: age 5s > horizon 2s -> BEYOND_HORIZON -> dropped, not re-applied

    assert sink.applied_total == 1  # NOT double-applied despite the expired dedup key (B4 closed)
    assert b.diagnostics().beyond_horizon_total >= 1
    assert await _pending(redis, config) == 0  # the aged-out entry was ACKed away (not stuck)


# =========================================================================== #
# T07/T08: a pending entry reclaimed WITHIN the horizon is suppressed by the durable dedup key
# =========================================================================== #
async def test_h8b_t07_t08_within_horizon_reclaim_suppressed_by_dedup(redis: Redis) -> None:
    config = _fast_idle(
        _config(
            max_redelivery_horizon_seconds=3_600,
            retention_safety_margin_seconds=0,
            dedup_ttl_seconds=7_200,
        )
    )
    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    await stream.publish(_envelope(seq=1))

    sink = RecordingShadowSink()
    crash_transport = _AckCrashTransport(RedisMarketEventStream(redis=redis, config=config))
    a = _consumer(redis, config, sink, now=_NOW, transport=crash_transport)
    await a.start()
    await a.poll_once()  # applied + durably marked; ACK lost -> pending. Dedup key NOT deleted.
    assert sink.applied_total == 1

    within = _NOW + timedelta(seconds=60)  # 60s < 3600s horizon: the dedup key still exists
    b = _consumer(redis, config, sink, now=within)
    await b.start()
    await asyncio.sleep(0.02)
    await (
        b.poll_once()
    )  # reclaim within horizon -> dedup contains True -> DUPLICATE, not re-applied

    assert sink.applied_total == 1
    assert b.diagnostics().duplicate_total >= 1
    assert b.diagnostics().beyond_horizon_total == 0  # within horizon, not dropped by the age gate
    assert await _pending(redis, config) == 0


# =========================================================================== #
# MEDIUM-1 fix: the consumer fails closed at start on a model_copy'd unsafe config
# =========================================================================== #
async def test_h8b_consumer_start_fails_closed_on_unsafe_config(redis: Redis) -> None:
    # model_copy bypasses the model validator; the consumer must re-check and refuse to start.
    unsafe = _config().model_copy(update={"dedup_ttl_seconds": 3_600})  # 3600 < 43200 + 3600
    consumer = _consumer(redis, unsafe, RecordingShadowSink(), now=_NOW)
    with pytest.raises(ValueError, match="B4 retention invariant"):
        await consumer.start()


# =========================================================================== #
# T14: a pending entry whose stream record was trimmed (MAXLEN/age) never crashes nor re-applies
# =========================================================================== #
async def test_h8b_t14_trimmed_pending_entry_is_not_reapplied(redis: Redis) -> None:
    config = _fast_idle(_config())
    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    await stream.publish(_envelope(seq=1))
    [(victim_id, _raw)] = await stream.read_raw()  # deliver into this consumer's PEL (unacked)
    assert await redis.xlen(config.stream_name) == 1

    # Deterministically evict the still-pending entry from the stream (models a trim past it).
    await redis.xtrim(config.stream_name, minid=_after(victim_id), approximate=False)
    assert await redis.xrange(config.stream_name, min=victim_id, max=victim_id) == []

    sink = RecordingShadowSink()
    consumer = _consumer(redis, config, sink, now=_NOW)
    await consumer.start()
    await asyncio.sleep(0.02)
    await consumer.poll_once()  # reclaim a trimmed pending entry: tombstone skipped, not applied
    await consumer.poll_once()  # a second cycle proves it does not jam the loop

    assert sink.applied_total == 0  # the trimmed entry is gone and never re-applied
    assert consumer.diagnostics().read_failures == 0  # reclaim did not error the cycle


# =========================================================================== #
# T05: the durable dedup key TTL exceeds the redelivery horizon (the invariant, realized)
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
    assert (
        ttl > config.max_redelivery_horizon_seconds
    )  # key survives past every within-horizon reclaim


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
    assert await durable.contains(epoch2) is False
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
