"""B11 loss detection end-to-end over real Redis (PHASE H8C).

The reconciliation taxonomy is unit-tested in ``tests/unit/test_market_ipc_loss_detection.py``.
This module proves ``RedisLossDetector`` against a disposable ``redislite`` server (never a shared/
production Redis): it reads only bounded Redis-native metadata (XINFO STREAM/GROUPS + one
XREVRANGE), reconciles it against producer L1 + consumer evidence, detects an injected reset /
rewind / tail-loss, fails closed when metadata is unavailable, treats a legal sequence gap and a
new producer epoch as healthy, and never issues an unbounded stream scan regardless of stream size.
No TickEngine/MarketContext authority, no Dhan, no production composition.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.market_ipc import (
    MarketEventEnvelope,
    MarketIpcConfig,
    RedisMarketEventStream,
    build_envelope,
)
from app.market_ipc.loss_detection import (
    ConsumerProgressEvidence,
    LossDetectionState,
    ProducerPublicationEvidence,
    RedisLossDetector,
)
from app.schemas.market_data import Instrument, Tick

redislite = pytest.importorskip("redislite", reason="disposable real Redis unavailable")

_NOW = datetime(2026, 9, 15, 6, 30, tzinfo=UTC)
_TD = date(2026, 9, 15)
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


def _envelope(*, seq: int, epoch: int = 1) -> MarketEventEnvelope:
    return build_envelope(
        Tick(
            instrument=Instrument(exchange="NSE", symbol="TCS"),
            event_timestamp=_NOW,
            last_price=Decimal(str(100 + seq)),
        ),
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW + timedelta(seconds=seq),
        trading_date=_TD,
        universe_version=7,
    )


def _producer(
    *, epoch: int = 1, last_published: int | None, terminal: bool = False, uncertain: bool = False
) -> ProducerPublicationEvidence:
    return ProducerPublicationEvidence(
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        last_published_sequence=last_published,
        terminal_publication_break=terminal,
        publication_outcome_uncertain=uncertain,
    )


async def _publish(
    stream: RedisMarketEventStream, *, start: int, count: int, epoch: int = 1
) -> list[str]:
    ids: list[str] = []
    for seq in range(start, start + count):
        ids.append(await stream.publish(_envelope(seq=seq, epoch=epoch)))
    return ids


# =========================================================================== #
# T01/T02/T03: healthy / consumer-lagging / pending over a real stream + group
# =========================================================================== #
async def test_h8c_t01_t02_t03_healthy_lagging_pending(redis: Redis) -> None:
    config = _config()
    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    await _publish(stream, start=1, count=10)
    detector = RedisLossDetector(redis, config)
    producer = _producer(last_published=10)

    # Nothing delivered yet: stream retains all 10, group at origin -> consumer lagging.
    lagging = await detector.evaluate(producer, ConsumerProgressEvidence(1, 0))
    assert lagging.state is LossDetectionState.CONSUMER_LAGGING
    assert lagging.ready_for_authority is True

    delivered = await stream.read_raw()  # deliver all 10 into the PEL (unacked -> pending)
    assert len(delivered) == 10
    pending = await detector.evaluate(producer, ConsumerProgressEvidence(1, 0))
    assert pending.state is LossDetectionState.PENDING_RECOVERY
    assert pending.pending == 10

    for message_id, _raw in delivered:
        await stream.ack(message_id)
    healthy = await detector.evaluate(producer, ConsumerProgressEvidence(1, 10))
    assert healthy.state is LossDetectionState.HEALTHY
    assert healthy.ready_for_authority is True


# =========================================================================== #
# T05: Redis reset (FLUSHALL) under a producer that has published -> fail closed
# =========================================================================== #
async def test_h8c_t05_redis_reset_detected(redis: Redis) -> None:
    config = _config()
    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    await _publish(stream, start=1, count=10)
    detector = RedisLossDetector(redis, config)
    producer = _producer(last_published=10)
    assert (await detector.evaluate(producer, ConsumerProgressEvidence(1, 10))).ready_for_authority

    await redis.flushall()  # isolated Redis state loss
    result = await detector.evaluate(producer, ConsumerProgressEvidence(1, 10))
    assert result.state is LossDetectionState.REDIS_STREAM_RESET
    assert result.ready_for_authority is False


async def test_h8c_t05_reset_detected_after_flushall_and_regroup(redis: Redis) -> None:
    # FLUSHALL then ensure_group recreates a fresh stream+group (last-generated-id 0-0) while the
    # producer L1 still says it published — exercises the stream-level reset signal with the group
    # PRESENT (not the group-absent fallback), even for a caught-up consumer.
    config = _config()
    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    await _publish(stream, start=1, count=10)
    await redis.flushall()
    await stream.ensure_group()  # fresh, empty stream + group

    detector = RedisLossDetector(redis, config)
    result = await detector.evaluate(_producer(last_published=10), ConsumerProgressEvidence(1, 10))
    assert result.state is LossDetectionState.REDIS_STREAM_RESET
    assert result.ready_for_authority is False


# =========================================================================== #
# T06: stream rewind — the group is durably ahead of the stream's last id
# =========================================================================== #
async def test_h8c_t06_stream_rewind_detected(redis: Redis) -> None:
    config = _config()
    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    await _publish(stream, start=1, count=10)
    info = await redis.xinfo_stream(config.stream_name)
    last_ms = int(
        str(
            info["last-generated-id"].decode()
            if isinstance(info["last-generated-id"], bytes)
            else info["last-generated-id"]
        ).split("-")[0]
    )
    # Model a restored-older-snapshot: the group checkpoint is ahead of every retained entry.
    await redis.xgroup_setid(config.stream_name, config.consumer_group, id=f"{last_ms + 10_000}-0")

    detector = RedisLossDetector(redis, config)
    result = await detector.evaluate(_producer(last_published=10), ConsumerProgressEvidence(1, 10))
    assert result.state is LossDetectionState.REDIS_STATE_REWIND
    assert result.ready_for_authority is False


# =========================================================================== #
# tail-loss: the confirmed tail is gone (XDEL) while the producer says it published it
# =========================================================================== #
async def test_h8c_tail_loss_detected(redis: Redis) -> None:
    config = _config()
    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    ids = await _publish(stream, start=1, count=10)
    await redis.xdel(config.stream_name, *ids[7:])  # drop seq 8,9,10 (the confirmed tail)

    detector = RedisLossDetector(redis, config)
    result = await detector.evaluate(_producer(last_published=10), ConsumerProgressEvidence(1, 5))
    assert result.state is LossDetectionState.PUBLISHED_EVENT_UNACCOUNTED_FOR
    assert result.ready_for_authority is False


# =========================================================================== #
# T08: a legal producer-sequence gap is NOT loss (only 100 and 102 published; 101 skipped)
# =========================================================================== #
async def test_h8c_t08_legal_sequence_gap_not_loss(redis: Redis) -> None:
    config = _config()
    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    await stream.publish(_envelope(seq=100))
    await stream.publish(_envelope(seq=102))  # 101 legally skipped (overflow/reject upstream)

    detector = RedisLossDetector(redis, config)
    result = await detector.evaluate(
        _producer(last_published=102), ConsumerProgressEvidence(1, 102)
    )
    assert result.state is LossDetectionState.HEALTHY  # never looks for the missing 101


# =========================================================================== #
# T09: a new producer epoch (sequence reset) is NOT a rewind
# =========================================================================== #
async def test_h8c_t09_new_epoch_not_rewind(redis: Redis) -> None:
    config = _config()
    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    await _publish(stream, start=1, count=5, epoch=1)
    await _publish(stream, start=1, count=3, epoch=2)  # new incarnation, sequence resets to 1

    detector = RedisLossDetector(redis, config)
    result = await detector.evaluate(
        _producer(epoch=2, last_published=3), ConsumerProgressEvidence(2, 3)
    )
    assert result.state is LossDetectionState.HEALTHY
    assert result.ready_for_authority is True


# =========================================================================== #
# T10: Redis restart with data preserved (new client, same socket) is healthy
# =========================================================================== #
async def test_h8c_t10_redis_restart_preserved_is_healthy(redis_socket: str) -> None:
    config = _config()
    first: Redis = Redis(unix_socket_path=redis_socket)
    await first.flushall()
    stream = RedisMarketEventStream(redis=first, config=config)
    await stream.ensure_group()
    await _publish(stream, start=1, count=10)
    await first.aclose()  # models a client/process restart; the Redis data persists

    reopened: Redis = Redis(unix_socket_path=redis_socket)
    try:
        detector = RedisLossDetector(reopened, config)
        result = await detector.evaluate(
            _producer(last_published=10), ConsumerProgressEvidence(1, 0)
        )
        assert result.state is LossDetectionState.CONSUMER_LAGGING  # data intact, consumer behind
        assert result.ready_for_authority is True
    finally:
        await reopened.aclose()


# =========================================================================== #
# T11: Redis metadata unavailable -> fail closed (never HEALTHY)
# =========================================================================== #
class _MetadataFailingRedis:
    """Redis wrapper whose XINFO raises a non-'no such key' error (metadata unavailable)."""

    def __init__(self, delegate: Redis) -> None:
        self._delegate = delegate

    async def xinfo_stream(self, *args: Any, **kwargs: Any) -> Any:
        raise RedisError("CLUSTERDOWN metadata unavailable")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


async def test_h8c_t11_metadata_unavailable_fails_closed(redis: Redis) -> None:
    config = _config()
    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    await _publish(stream, start=1, count=3)
    detector = RedisLossDetector(_MetadataFailingRedis(redis), config)  # type: ignore[arg-type]
    result = await detector.evaluate(_producer(last_published=3), ConsumerProgressEvidence(1, 3))
    assert result.state is LossDetectionState.INSUFFICIENT_EVIDENCE
    assert result.ready_for_authority is False


# =========================================================================== #
# T22: bounded metadata reads — no unbounded stream scan regardless of stream size
# =========================================================================== #
class _CountingRedis:
    """Counts Redis calls and forbids full-stream scans (xrange/xread) during evaluation."""

    def __init__(self, delegate: Redis) -> None:
        self._delegate = delegate
        self.calls: dict[str, int] = {}

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._delegate, name)
        if name in {"xrange", "xread", "xreadgroup"}:
            raise AssertionError(f"loss detection must not call {name} (unbounded scan)")
        if not callable(attr):
            return attr

        async def _counted(*args: Any, **kwargs: Any) -> Any:
            self.calls[name] = self.calls.get(name, 0) + 1
            return await attr(*args, **kwargs)

        return _counted


async def test_h8c_t22_metadata_reads_are_bounded(redis: Redis) -> None:
    config = _config(maxlen=1_000_000)
    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    await _publish(stream, start=1, count=2_000)  # large stream

    counting = _CountingRedis(redis)
    detector = RedisLossDetector(counting, config)  # type: ignore[arg-type]
    result = await detector.evaluate(
        _producer(last_published=2_000), ConsumerProgressEvidence(1, 0)
    )
    assert result.state is LossDetectionState.CONSUMER_LAGGING
    # A constant, small number of metadata calls — independent of the 2,000-entry stream size.
    assert counting.calls.get("xinfo_stream", 0) == 1
    assert counting.calls.get("xinfo_groups", 0) == 1
    assert counting.calls.get("xrevrange", 0) == 1
    assert sum(counting.calls.values()) <= 4


# =========================================================================== #
# T26/T28: large replay is healthy-equivalent and deterministic across reruns
# =========================================================================== #
async def test_h8c_t26_t28_large_replay_deterministic(redis: Redis) -> None:
    config = _config(maxlen=1_000_000)
    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    await _publish(stream, start=1, count=10_000)

    detector = RedisLossDetector(redis, config)
    producer = _producer(last_published=10_000)
    verdicts = {
        (await detector.evaluate(producer, ConsumerProgressEvidence(1, 0))).state for _ in range(3)
    }
    assert verdicts == {LossDetectionState.CONSUMER_LAGGING}  # stream retains all; consumer behind


# =========================================================================== #
# T18: duplicate transport entries do not create a false loss/corruption alarm
# =========================================================================== #
async def test_h8c_t18_duplicate_transport_not_false_alarm(redis: Redis) -> None:
    config = _config()
    stream = RedisMarketEventStream(redis=redis, config=config)
    await stream.ensure_group()
    await _publish(stream, start=1, count=5)
    await stream.publish(
        _envelope(seq=5)
    )  # re-publish identity (5): a legal at-least-once duplicate

    detector = RedisLossDetector(redis, config)
    result = await detector.evaluate(_producer(last_published=5), ConsumerProgressEvidence(1, 5))
    assert result.state is LossDetectionState.HEALTHY
    assert result.ready_for_authority is True
