"""Real disposable-Redis integration for the shadow backend consumer (DECOUPLING PHASE C).

Runs a real ``redis-server`` (bundled by ``redislite`` on a private unix socket) — never a
shared or production Redis. Proves the consumer over actual stream primitives (XREADGROUP/
XPENDING/XACK/XAUTOCLAIM), that a poison message cannot jam the group, that a pending backlog
larger than ``read_count`` is recovered across bounded cycles, consumer restart/redelivery,
ordering, and Redis-unavailable isolation. Skips cleanly if redislite is absent.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis

from app.market_ipc import (
    MarketEventConsumer,
    MarketEventEnvelope,
    MarketIpcConfig,
    RecordingShadowSink,
    RedisMarketEventStream,
    build_envelope,
)
from app.schemas.market_data import Instrument, Tick

redislite = pytest.importorskip("redislite", reason="disposable real Redis unavailable")

_NOW = datetime(2026, 9, 9, 10, 15, 30, tzinfo=UTC)
_TD = date(2026, 9, 9)
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


def _tick(symbol: str = "TCS", price: str = "100.5") -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        event_timestamp=_NOW,
        last_price=Decimal(price),
    )


def _envelope(payload: Tick, *, seq: int, epoch: int = 1) -> MarketEventEnvelope:
    return build_envelope(
        payload,
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=7,
    )


def _config(**overrides: object) -> MarketIpcConfig:
    # block_ms=0: non-blocking reads keep the disposable-Redis suite fast (production long-polls).
    return MarketIpcConfig(block_ms=0, **overrides)


def _consumer(
    redis: Redis, config: MarketIpcConfig, sink: RecordingShadowSink
) -> MarketEventConsumer:
    return MarketEventConsumer(
        transport=RedisMarketEventStream(redis=redis, config=config),
        config=config,
        sink=sink,
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: 7,
        now=lambda: _NOW,
    )


# --------------------------------------------------------------------------- #
# End-to-end: XREADGROUP -> apply -> XACK -> XPENDING cleared
# --------------------------------------------------------------------------- #
async def test_consume_applies_and_acks_end_to_end(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    for i in range(1, 6):
        await producer.publish(_envelope(_tick(), seq=i))

    sink = RecordingShadowSink()
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await consumer.poll_once()

    assert sink.applied_total == 5
    assert consumer.diagnostics().acked_total == 5
    pending = await redis.xpending(config.stream_name, config.consumer_group)
    assert pending["pending"] == 0


async def test_poison_message_does_not_jam_the_group(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await redis.xadd(config.stream_name, {"e": b"not-a-valid-envelope"})  # poison
    await producer.publish(_envelope(_tick(), seq=1))  # valid, behind the poison

    sink = RecordingShadowSink()
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await consumer.poll_once()

    assert sink.applied_total == 1  # valid event processed despite the poison ahead of it
    assert consumer.diagnostics().envelope_decode_failures == 1
    pending = await redis.xpending(config.stream_name, config.consumer_group)
    assert pending["pending"] == 0  # poison terminally ACKed, group not jammed


# --------------------------------------------------------------------------- #
# XAUTOCLAIM: pending backlog larger than read_count, recovered across bounded cycles
# --------------------------------------------------------------------------- #
async def test_pending_beyond_read_count_recovers_across_bounded_cycles(redis: Redis) -> None:
    base = _config(read_count=100)
    producer = RedisMarketEventStream(redis=redis, config=base)
    await producer.ensure_group()
    for i in range(1, 251):
        await producer.publish(_envelope(_tick(), seq=i))

    dead = RedisMarketEventStream(
        redis=redis, config=base.model_copy(update={"consumer_name": "dead"})
    )
    while await dead.read_raw():  # drain all 250 into the dead consumer's PEL, never acked
        pass

    recover_config = base.model_copy(update={"consumer_name": "recover", "claim_idle_ms": 1})
    sink = RecordingShadowSink()
    consumer = _consumer(redis, recover_config, sink)
    await consumer.start()
    await asyncio.sleep(0.02)  # let pending exceed the 1ms idle threshold

    per_cycle: list[int] = []
    for _ in range(6):
        before = consumer.diagnostics().applied_total
        await consumer.poll_once()
        per_cycle.append(consumer.diagnostics().applied_total - before)
        if consumer.diagnostics().applied_total >= 250:
            break

    assert consumer.diagnostics().applied_total == 250  # all recovered
    assert max(per_cycle) <= 100  # bounded work per cycle (no unbounded single-cycle drain)
    assert len([c for c in per_cycle if c > 0]) >= 3  # 250/100 -> needed multiple cycles


async def test_consumer_restart_redelivers_unacked_entry(redis: Redis) -> None:
    base = _config()
    producer = RedisMarketEventStream(redis=redis, config=base)
    await producer.ensure_group()
    await producer.publish(_envelope(_tick(), seq=1))

    dead = RedisMarketEventStream(
        redis=redis, config=base.model_copy(update={"consumer_name": "dead"})
    )
    await dead.read_raw()  # reads but never acks (simulated crash)

    recover_config = base.model_copy(update={"consumer_name": "recover", "claim_idle_ms": 1})
    sink = RecordingShadowSink()
    consumer = _consumer(redis, recover_config, sink)
    await consumer.start()
    await asyncio.sleep(0.02)
    await consumer.poll_once()

    assert sink.applied_total == 1  # reclaimed and applied by the restarted consumer
    pending = await redis.xpending(base.stream_name, base.consumer_group)
    assert pending["pending"] == 0


async def test_global_stream_order_is_preserved(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    # One global producer numbers all instruments in a single monotonic sequence.
    symbols = ["A", "B", "A", "B"]
    for seq, symbol in enumerate(symbols, start=1):
        await producer.publish(_envelope(_tick(symbol), seq=seq))

    sink = RecordingShadowSink()
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await consumer.poll_once()

    applied = [
        (event.instrument.symbol, envelope.producer_sequence) for envelope, event in sink.events
    ]
    assert applied == [("A", 1), ("B", 2), ("A", 3), ("B", 4)]  # global-stream order preserved


async def test_redis_unavailable_is_isolated_and_counted() -> None:
    config = _config()
    broken = Redis(unix_socket_path="/nonexistent/apexscan-redis.sock")
    sink = RecordingShadowSink()
    consumer = _consumer(broken, config, sink)
    await consumer.poll_once()  # must not raise
    assert consumer.diagnostics().read_failures == 1
    assert sink.applied_total == 0
    await broken.aclose()
