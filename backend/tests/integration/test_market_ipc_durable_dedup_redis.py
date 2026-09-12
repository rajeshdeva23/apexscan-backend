"""Real disposable-Redis integration for durable consumer idempotency (DECOUPLING PHASE C1).

Runs a real ``redis-server`` (bundled by ``redislite`` on a private unix socket) — never a
shared or production Redis. Proves the properties that only a durable store can provide: a
completed application is recognised by a FRESH consumer/process (empty in-memory cache) over the
same Redis, the same canonical identity carried by a DIFFERENT Redis Stream id is suppressed,
the dedup key carries a bounded TTL, and a dedup-store failure is fail-closed (left pending,
never applied). Skips cleanly if redislite is absent.
"""

from __future__ import annotations

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
    dedup_key,
)
from app.market_ipc.envelope import ProducerEventIdentity
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


def _envelope(*, seq: int, epoch: int = 1) -> MarketEventEnvelope:
    return build_envelope(
        _tick(),
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=7,
    )


def _config(**overrides: object) -> MarketIpcConfig:
    return MarketIpcConfig(block_ms=0, **overrides)


def _durable_consumer(
    redis: Redis, config: MarketIpcConfig, sink: RecordingShadowSink
) -> MarketEventConsumer:
    deduplicator = CompositeDeduplicator(
        memory=BoundedDeduplicator(config.dedup_max_entries),
        durable=DurableDeduplicator(redis, config),
    )
    return MarketEventConsumer(
        transport=RedisMarketEventStream(redis=redis, config=config),
        config=config,
        sink=sink,
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: 7,
        now=lambda: _NOW,
        deduplicator=deduplicator,
    )


async def test_fresh_consumer_suppresses_already_applied_identity(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    first_stream_id = await producer.publish(_envelope(seq=1))

    first = _durable_consumer(redis, config, RecordingShadowSink())
    await first.start()
    await first.poll_once()
    assert first.diagnostics().applied_total == 1

    # A DIFFERENT Redis Stream id carrying the SAME canonical identity (at-least-once re-publish),
    # consumed by a FRESH consumer whose in-memory cache is empty (simulated process restart).
    second_stream_id = await producer.publish(_envelope(seq=1))
    assert second_stream_id != first_stream_id  # genuinely a distinct transport id
    second = _durable_consumer(redis, config, RecordingShadowSink())
    await second.start()
    await second.poll_once()

    assert second.diagnostics().applied_total == 0  # durable store recognised it across processes
    assert second.diagnostics().duplicate_total == 1


async def test_dedup_key_carries_bounded_ttl(redis: Redis) -> None:
    config = _config(dedup_ttl_seconds=3_600)
    identity = ProducerEventIdentity(_PRODUCER, 1, 1)
    await DurableDeduplicator(redis, config).record(identity)

    ttl = await redis.ttl(dedup_key(config.dedup_key_prefix, identity))
    assert 0 < ttl <= 3_600  # bounded: storage cannot grow without limit


async def test_dedup_store_failure_is_fail_closed_and_counted() -> None:
    config = _config()
    broken = Redis(unix_socket_path="/nonexistent/apexscan-dedup.sock")
    sink = RecordingShadowSink()
    # Transport reads/acks are stubbed as healthy so ONLY the dedup store is down; the point is
    # that a dedup-store failure must not apply blind — it leaves the entry pending.
    consumer = MarketEventConsumer(
        transport=_HealthyTransportOneEntry(),  # type: ignore[arg-type]
        config=config,
        sink=sink,
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: 7,
        now=lambda: _NOW,
        deduplicator=DurableDeduplicator(broken, config),
    )
    await consumer.poll_once()

    assert sink.applied_total == 0  # never applied without idempotency protection
    assert consumer.diagnostics().dedup_store_failures == 1
    assert consumer.diagnostics().acked_total == 0
    await broken.aclose()


class _HealthyTransportOneEntry:
    """Transport double delivering one valid entry with working ack (isolates dedup failure)."""

    def __init__(self) -> None:
        self._raw = _envelope(seq=1).model_dump_json().encode("utf-8")
        self._delivered = False
        self.acked: list[str] = []

    async def ensure_group(self) -> None:
        return None

    async def read_raw(self) -> list[tuple[str, bytes]]:
        if self._delivered:
            return []
        self._delivered = True
        return [("0-1", self._raw)]

    async def claim_page_raw(self, start_id: str) -> tuple[str, list[tuple[str, bytes]]]:
        return "0-0", []

    async def ack(self, *message_ids: str) -> int:
        self.acked.extend(message_ids)
        return len(message_ids)
