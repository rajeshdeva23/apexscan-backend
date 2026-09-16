"""md:health conveyance + B11 loss-detection composed in the consumer runtime (DECOUPLING H9B).

Two offline proofs over a disposable ``redislite`` server:

* **md:health round-trip** — the producer L1 :class:`FeedContinuitySnapshot` is projected to the
  single :class:`IngestionHealthState` truth, written to ``md:health``, read back, and re-projected
  to :class:`ProducerPublicationEvidence` identical to ``from_continuity`` — across healthy /
  terminal-break / uncertain states, and fail-closed (``None``) on missing / stale / malformed.
* **B11 composed** — the real shadow :class:`MarketEventConsumerRuntime` (built by
  ``compose_consumer_runtime``) reconciles producer evidence (from ``md:health``), its own durably
  applied ``(epoch, sequence)``, and bounded Redis metadata into an authority-readiness INPUT that
  fails closed on a producer break, a missing/stale health snapshot, or a Redis stream loss.

OFFLINE and NON-AUTHORITATIVE: no Dhan, no production, no authority activation; the aware-UTC clock
is injected; skips cleanly without redislite.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from redis.asyncio import Redis

from app.market_ingestion.mode import MarketPathMode, PhaseHFlags
from app.market_ipc import (
    ConsumerProgressEvidence,
    FeedContinuityTracker,
    IngestionHealthPublisher,
    IngestionHealthReader,
    LossDetectionState,
    MarketEventConsumerRuntime,
    MarketEventEnvelope,
    MarketIpcConfig,
    ProducerPublicationEvidence,
    RecordingShadowSink,
    RedisMarketEventStream,
    build_envelope,
    compose_consumer_runtime,
    health_key,
    ingestion_health_from_continuity,
)
from app.market_ipc.state import IngestionHealthState
from app.schemas.market_data import Instrument, Tick

redislite = pytest.importorskip("redislite", reason="disposable real Redis unavailable")

_NOW = datetime(2026, 9, 9, 10, 15, 30, tzinfo=UTC)
_TD = date(2026, 9, 9)
_PRODUCER = "market-ingestion"
_EPOCH = 1


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


def _tick(symbol: str = "TCS") -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        event_timestamp=_NOW,
        last_price=Decimal("100.5"),
    )


def _envelope(*, seq: int, epoch: int = _EPOCH) -> MarketEventEnvelope:
    return build_envelope(
        _tick(),
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=7,
    )


def _healthy_snapshot(*, last_published: int = 5) -> object:
    tracker = FeedContinuityTracker()
    tracker.producer_started(producer_id=_PRODUCER, producer_epoch=_EPOCH)
    tracker.provider_connected()
    tracker.publication_succeeded(producer_sequence=last_published)
    return tracker.snapshot()


def _broken_snapshot() -> object:
    tracker = FeedContinuityTracker()
    tracker.producer_started(producer_id=_PRODUCER, producer_epoch=_EPOCH)
    tracker.provider_connected()
    tracker.publication_succeeded(producer_sequence=5)
    tracker.publication_failed()  # terminal break
    return tracker.snapshot()


def _uncertain_snapshot() -> object:
    tracker = FeedContinuityTracker()
    tracker.producer_started(producer_id=_PRODUCER, producer_epoch=_EPOCH)
    tracker.provider_connected()
    tracker.publication_uncertain()  # terminal, outcome unknown
    return tracker.snapshot()


async def _wait_until(predicate: Callable[[], bool], *, limit: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached before timeout")


# =========================================================================== #
# md:health round-trip: producer snapshot -> md:health -> backend evidence
# =========================================================================== #
@pytest.mark.parametrize("builder", [_healthy_snapshot, _broken_snapshot, _uncertain_snapshot])
async def test_health_round_trip_preserves_producer_evidence(redis: Redis, builder) -> None:
    config = MarketIpcConfig()
    snapshot = builder()
    state = ingestion_health_from_continuity(snapshot, updated_at=_NOW)
    assert await IngestionHealthPublisher(redis, config).publish(state) is True

    evidence = await IngestionHealthReader(redis, config).read_evidence(_NOW)
    assert evidence == ProducerPublicationEvidence.from_continuity(snapshot)


async def test_health_missing_is_fail_closed(redis: Redis) -> None:
    reader = IngestionHealthReader(redis, MarketIpcConfig())
    assert await reader.read() is None
    assert await reader.read_evidence(_NOW) is None


async def test_health_stale_is_fail_closed(redis: Redis) -> None:
    config = MarketIpcConfig()
    state = ingestion_health_from_continuity(_healthy_snapshot(), updated_at=_NOW)
    await IngestionHealthPublisher(redis, config).publish(state)
    later = _NOW + timedelta(seconds=config.health_stale_seconds + 1)
    assert await IngestionHealthReader(redis, config).read_evidence(later) is None


async def test_health_malformed_is_fail_closed(redis: Redis) -> None:
    config = MarketIpcConfig()
    await redis.set(health_key(config), "not-json{")
    reader = IngestionHealthReader(redis, config)
    assert await reader.read() is None
    assert await reader.read_evidence(_NOW) is None


async def test_health_ttl_is_applied(redis: Redis) -> None:
    config = MarketIpcConfig()
    state = ingestion_health_from_continuity(_healthy_snapshot(), updated_at=_NOW)
    await IngestionHealthPublisher(redis, config).publish(state)
    ttl = await redis.ttl(health_key(config))
    assert 0 < ttl <= config.health_ttl_seconds


# =========================================================================== #
# B11 composed in the shadow runtime: authority-readiness INPUT (activates nothing)
# =========================================================================== #
async def _runtime(redis_socket: str) -> MarketEventConsumerRuntime:
    return await compose_consumer_runtime(
        _Settings(redis_socket, flags=_shadow_flags()),
        sink=RecordingShadowSink(),
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: 7,
        now=lambda: _NOW,
    )


async def _publish_health(redis: Redis, snapshot: object, *, updated_at: datetime = _NOW) -> None:
    state = ingestion_health_from_continuity(snapshot, updated_at=updated_at)
    await IngestionHealthPublisher(redis, MarketIpcConfig()).publish(state)


async def test_b11_healthy_when_consumer_caught_up(redis: Redis, redis_socket: str) -> None:
    config = MarketIpcConfig(block_ms=0)
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    for seq in range(1, 6):
        await producer.publish(_envelope(seq=seq))
    await _publish_health(redis, _healthy_snapshot(last_published=5))

    runtime = await _runtime(redis_socket)
    await runtime.start()
    try:
        await _wait_until(lambda: runtime.diagnostics().applied_total >= 5)
        result = await runtime.evaluate_authority_readiness()
    finally:
        await runtime.stop()

    assert result.state is LossDetectionState.HEALTHY
    assert result.ready_for_authority is True
    assert result.consumer_last_applied_sequence == 5


async def test_b11_consumer_lagging_is_ready(redis: Redis, redis_socket: str) -> None:
    config = MarketIpcConfig(block_ms=0)
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    for seq in range(1, 6):
        await producer.publish(_envelope(seq=seq))
    await _publish_health(redis, _healthy_snapshot(last_published=5))

    runtime = await _runtime(redis_socket)  # composed but NOT started → nothing applied
    result = await runtime.evaluate_authority_readiness()
    await runtime.stop()

    assert result.state is LossDetectionState.CONSUMER_LAGGING
    assert result.ready_for_authority is True


async def test_b11_producer_break_is_fail_closed(redis: Redis, redis_socket: str) -> None:
    config = MarketIpcConfig(block_ms=0)
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(seq=1))
    await _publish_health(redis, _broken_snapshot())

    runtime = await _runtime(redis_socket)
    result = await runtime.evaluate_authority_readiness()
    await runtime.stop()

    assert result.state is LossDetectionState.PRODUCER_PUBLICATION_FAILED
    assert result.ready_for_authority is False


async def test_b11_missing_health_is_fail_closed(redis: Redis, redis_socket: str) -> None:
    config = MarketIpcConfig(block_ms=0)
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(seq=1))  # events exist, but no md:health snapshot

    runtime = await _runtime(redis_socket)
    result = await runtime.evaluate_authority_readiness()
    await runtime.stop()

    assert result.state is LossDetectionState.INSUFFICIENT_EVIDENCE
    assert result.ready_for_authority is False


async def test_b11_redis_stream_loss_is_fail_closed(redis: Redis, redis_socket: str) -> None:
    config = MarketIpcConfig(block_ms=0)
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    for seq in range(1, 6):
        await producer.publish(_envelope(seq=seq))
    await _publish_health(redis, _healthy_snapshot(last_published=5))
    await redis.delete(config.stream_name)  # the confirmed stream vanished under a live producer

    runtime = await _runtime(redis_socket)
    result = await runtime.evaluate_authority_readiness()
    await runtime.stop()

    assert result.state is LossDetectionState.REDIS_STREAM_RESET
    assert result.ready_for_authority is False


async def test_b11_inert_runtime_is_fail_closed(redis_socket: str) -> None:
    legacy_flags = PhaseHFlags(
        market_ingestion_service_enabled=False,
        ipc_publisher_enabled=False,
        ipc_consumer_enabled=False,
        ipc_shadow_compare_enabled=False,
        ipc_authoritative_enabled=False,
        legacy_market_path_enabled=True,
    )
    runtime = await compose_consumer_runtime(_Settings(redis_socket, flags=legacy_flags))
    assert runtime.mode is MarketPathMode.LEGACY_ONLY
    result = await runtime.evaluate_authority_readiness()
    assert result.state is LossDetectionState.INSUFFICIENT_EVIDENCE
    assert result.ready_for_authority is False


# =========================================================================== #
# Consumer last-applied identity (B11 evidence input) advances only on apply
# =========================================================================== #
async def test_consumer_progress_advances_monotonically(redis: Redis, redis_socket: str) -> None:
    config = MarketIpcConfig(block_ms=0)
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    for seq in range(1, 4):
        await producer.publish(_envelope(seq=seq))

    runtime = await _runtime(redis_socket)
    await runtime.start()
    try:
        await _wait_until(lambda: runtime.diagnostics().applied_total >= 3)
    finally:
        await runtime.stop()

    diagnostics = runtime.diagnostics()
    assert diagnostics is not None
    assert diagnostics.last_applied_epoch == _EPOCH
    assert diagnostics.last_applied_sequence == 3


def test_evidence_from_ingestion_health_matches_from_continuity() -> None:
    snapshot = _healthy_snapshot(last_published=9)
    state: IngestionHealthState = ingestion_health_from_continuity(snapshot, updated_at=_NOW)
    assert ProducerPublicationEvidence.from_ingestion_health(
        state
    ) == ProducerPublicationEvidence.from_continuity(snapshot)


def test_ingestion_health_state_carries_b11_fields() -> None:
    state = ingestion_health_from_continuity(_broken_snapshot(), updated_at=_NOW)
    assert state.terminal_publication_break is True
    assert state.publication_outcome_uncertain is False
    assert state.last_published_sequence == 5


def test_consumer_progress_evidence_default_is_empty() -> None:
    assert ConsumerProgressEvidence() == ConsumerProgressEvidence(
        last_applied_epoch=None, last_applied_sequence=None
    )
