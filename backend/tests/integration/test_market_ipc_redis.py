"""Real disposable-Redis integration for the IPC transport + publisher (PHASE B).

Runs a real ``redis-server`` (bundled by ``redislite`` on a private unix socket) — never a
shared or production Redis. Verifies the actual stream primitives the frozen architecture
relies on (XGROUP/XADD/XREADGROUP/XACK/XPENDING/XAUTOCLAIM/MAXLEN), redelivery, ordering,
atomic epoch allocation, and the publisher end-to-end. Skips cleanly if redislite is absent.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis

from app.market_ipc import (
    EPOCH_KEY_PREFIX,
    MarketEventPublisher,
    MarketIpcConfig,
    RedisEpochAllocator,
    RedisMarketEventStream,
    StaticUniverseVersion,
    build_envelope,
    decode_payload,
)
from app.schemas.market_data import (
    FeedContinuity,
    FeedContinuityEvent,
    Instrument,
    MarketReference,
    Quote,
    Tick,
)

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


def _instrument(symbol: str = "TCS") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _tick(symbol: str = "TCS", *, seq: int, epoch: int = 1) -> object:
    payload = Tick(
        instrument=_instrument(symbol), event_timestamp=_NOW, last_price=Decimal("100.5")
    )
    return build_envelope(
        payload,
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=7,
    )


def _stream(redis: Redis, **overrides: object) -> RedisMarketEventStream:
    return RedisMarketEventStream(redis=redis, config=MarketIpcConfig(**overrides))


# --------------------------------------------------------------------------- #
# XGROUP / XADD / XREADGROUP / XACK / XPENDING
# --------------------------------------------------------------------------- #
async def test_ensure_group_is_idempotent_and_creates_stream(redis: Redis) -> None:
    stream = _stream(redis)
    await stream.ensure_group()
    await stream.ensure_group()  # BUSYGROUP tolerated
    groups = await redis.xinfo_groups(MarketIpcConfig().stream_name)
    names = {g.get("name", g.get(b"name")) for g in groups}
    assert "backend" in names or b"backend" in names


async def test_publish_read_roundtrip_preserves_order_and_payload(redis: Redis) -> None:
    stream = _stream(redis)
    await stream.ensure_group()
    for i in range(1, 51):
        await stream.publish(_tick(seq=i))
    delivered = await stream.read()
    assert [env.producer_sequence for _mid, env in delivered] == list(range(1, 51))
    payload = decode_payload(delivered[0][1].event_kind, delivered[0][1].payload)
    assert payload.last_price == Decimal("100.5")  # lossless through real Redis


async def test_ack_clears_pending(redis: Redis) -> None:
    stream = _stream(redis)
    await stream.ensure_group()
    await stream.publish(_tick(seq=1))
    await stream.publish(_tick(seq=2))
    delivered = await stream.read()
    pending_before = await redis.xpending(MarketIpcConfig().stream_name, "backend")
    assert pending_before["pending"] == 2
    acked = await stream.ack(*[mid for mid, _env in delivered])
    assert acked == 2
    pending_after = await redis.xpending(MarketIpcConfig().stream_name, "backend")
    assert pending_after["pending"] == 0


# --------------------------------------------------------------------------- #
# XAUTOCLAIM redelivery / consumer restart
# --------------------------------------------------------------------------- #
async def test_unacked_entries_are_reclaimable(redis: Redis) -> None:
    config = MarketIpcConfig(consumer_name="consumer-a", claim_idle_ms=1_000)
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_tick(seq=1))
    await producer.read()  # consumer-a reads but never acks
    reclaimer = RedisMarketEventStream(
        redis=redis,
        config=config.model_copy(update={"consumer_name": "consumer-b", "claim_idle_ms": 1}),
    )
    await asyncio.sleep(0.02)  # let the entry exceed the 1ms idle threshold
    claimed = await reclaimer.claim_stale()
    assert [env.producer_sequence for _mid, env in claimed] == [1]  # redelivered to consumer-b


# --------------------------------------------------------------------------- #
# MAXLEN bounded trimming
# --------------------------------------------------------------------------- #
async def test_maxlen_bounds_stream_growth(redis: Redis) -> None:
    stream = _stream(redis, maxlen=1_000)  # floor is 1000; approximate trimming
    await stream.ensure_group()
    for i in range(1, 5_001):
        await stream.publish(_tick(seq=i))
    length = await redis.xlen(MarketIpcConfig().stream_name)
    assert length < 5_000  # trimming happened (unbounded growth prevented)
    assert length <= 2_000  # approximate MAXLEN stays within a node-rounded bound of 1000


# --------------------------------------------------------------------------- #
# atomic epoch allocation
# --------------------------------------------------------------------------- #
async def test_epoch_allocation_is_monotonic_across_restarts(redis: Redis) -> None:
    allocator = RedisEpochAllocator(redis)
    first = await allocator.allocate(_PRODUCER)
    second = await allocator.allocate(_PRODUCER)  # simulates a second run
    assert (first, second) == (1, 2)
    assert await redis.get(f"{EPOCH_KEY_PREFIX}:{_PRODUCER}") == b"2"


async def test_concurrent_epoch_allocation_is_unique(redis: Redis) -> None:
    import asyncio

    allocator = RedisEpochAllocator(redis)
    epochs = await asyncio.gather(*(allocator.allocate("p") for _ in range(20)))
    assert sorted(epochs) == list(range(1, 21))  # atomic INCR: all distinct


# --------------------------------------------------------------------------- #
# publisher end-to-end through real Redis
# --------------------------------------------------------------------------- #
async def test_publisher_end_to_end_all_kinds(redis: Redis) -> None:
    config = MarketIpcConfig()
    stream = RedisMarketEventStream(redis=redis, config=config)
    publisher = MarketEventPublisher(
        stream=stream,
        config=config,
        producer_id=_PRODUCER,
        epoch_allocator=RedisEpochAllocator(redis),
        trading_date_source=_FixedDate(),
        universe_version_source=StaticUniverseVersion(7),
        now=lambda: _NOW,
    )
    await publisher.start()
    originals = [
        Tick(instrument=_instrument(), event_timestamp=_NOW, last_price=Decimal("101.25")),
        Quote(
            instrument=_instrument(),
            event_timestamp=_NOW,
            bid_price=Decimal("101.10"),
            ask_price=Decimal("101.40"),
            bid_quantity=10,
            ask_quantity=20,
        ),
        MarketReference(instrument=_instrument(), previous_close=Decimal("100.00")),
        FeedContinuityEvent(status=FeedContinuity.CONNECTED, observed_at=_NOW),
    ]
    for datum in originals:
        await publisher.publish(datum)
    delivered = await stream.read()
    restored = [decode_payload(env.event_kind, env.payload) for _mid, env in delivered]
    assert restored == originals
    assert publisher.diagnostics().events_published_total == 4


class _FixedDate:
    def current_trading_date(self) -> date:
        return _TD
