"""Unit tests for durable consumer idempotency primitives (DECOUPLING PHASE C1).

Covers the canonical dedup key (collision-freedom + transport-id independence), the in-memory
async adapter, the composite (durable authority fronted by an in-memory cache), and the durable
Redis adapter's key/TTL contract and fail-closed error propagation — all against fakes so the
suite stays fast. Real-Redis durability/restart/retention is proven by the integration suite.
"""

from __future__ import annotations

import pytest
from redis.exceptions import RedisError

from app.market_ipc import (
    BoundedDeduplicator,
    CompositeDeduplicator,
    DurableDeduplicator,
    InMemoryDeduplicator,
    MarketIpcConfig,
    dedup_key,
)
from app.market_ipc.envelope import ProducerEventIdentity


def _identity(
    producer: str = "market-ingestion", epoch: int = 1, seq: int = 1
) -> ProducerEventIdentity:
    return ProducerEventIdentity(producer, epoch, seq)


# --------------------------------------------------------------------------- #
# dedup_key: collision-free + transport-id independent
# --------------------------------------------------------------------------- #
def test_dedup_key_distinguishes_each_identity_field() -> None:
    base = dedup_key("md:dedup", _identity())
    assert base != dedup_key("md:dedup", _identity(epoch=2))
    assert base != dedup_key("md:dedup", _identity(seq=2))
    assert base != dedup_key("md:dedup", _identity(producer="other"))


def test_dedup_key_is_collision_free_when_producer_id_contains_delimiter() -> None:
    # length-prefixing prevents "a:1" + epoch 2 colliding with "a" + epoch "1:2"-style splits.
    left = dedup_key("md:dedup", ProducerEventIdentity("a:1", 2, 3))
    right = dedup_key("md:dedup", ProducerEventIdentity("a", 12, 3))
    assert left != right


def test_dedup_key_ignores_transport_stream_id() -> None:
    # The key is a pure function of the canonical identity; there is no stream-id input at all.
    assert dedup_key("md:dedup", _identity()) == dedup_key("md:dedup", _identity())


# --------------------------------------------------------------------------- #
# InMemoryDeduplicator: async adapter over the bounded window
# --------------------------------------------------------------------------- #
async def test_in_memory_records_and_contains() -> None:
    dedup = InMemoryDeduplicator(BoundedDeduplicator(max_entries=10))
    identity = _identity()
    assert await dedup.contains(identity) is False
    await dedup.record(identity)
    assert await dedup.contains(identity) is True


# --------------------------------------------------------------------------- #
# DurableDeduplicator: key/TTL contract + fail-closed propagation
# --------------------------------------------------------------------------- #
class _FakeRedis:
    """Minimal async Redis double recording exists/set calls, with optional fault injection."""

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, bytes] = {}
        self.set_calls: list[tuple[str, bytes, int | None]] = []
        self._fail = fail

    async def exists(self, key: str) -> int:
        if self._fail:
            raise RedisError("down")
        return 1 if key in self.store else 0

    async def set(self, key: str, value: bytes, *, ex: int | None = None) -> None:
        if self._fail:
            raise RedisError("down")
        self.store[key] = value
        self.set_calls.append((key, value, ex))


async def test_durable_record_sets_key_with_configured_ttl() -> None:
    redis = _FakeRedis()
    config = MarketIpcConfig(dedup_key_prefix="md:dedup", dedup_ttl_seconds=3_600)
    dedup = DurableDeduplicator(redis, config)  # type: ignore[arg-type]
    identity = _identity()

    assert await dedup.contains(identity) is False
    await dedup.record(identity)
    assert await dedup.contains(identity) is True
    key, value, ttl = redis.set_calls[0]
    assert key == dedup_key("md:dedup", identity)
    assert value == b"1"
    assert ttl == 3_600


async def test_durable_contains_propagates_redis_error() -> None:
    dedup = DurableDeduplicator(_FakeRedis(fail=True), MarketIpcConfig())  # type: ignore[arg-type]
    with pytest.raises(RedisError):
        await dedup.contains(_identity())


async def test_durable_record_propagates_redis_error() -> None:
    dedup = DurableDeduplicator(_FakeRedis(fail=True), MarketIpcConfig())  # type: ignore[arg-type]
    with pytest.raises(RedisError):
        await dedup.record(_identity())


# --------------------------------------------------------------------------- #
# CompositeDeduplicator: durable authority + in-memory cache
# --------------------------------------------------------------------------- #
def _composite(redis: _FakeRedis) -> CompositeDeduplicator:
    config = MarketIpcConfig()
    return CompositeDeduplicator(
        memory=BoundedDeduplicator(max_entries=10),
        durable=DurableDeduplicator(redis, config),  # type: ignore[arg-type]
    )


async def test_composite_sees_identity_recorded_by_a_prior_process() -> None:
    # A previous process recorded the identity in the shared durable store; this composite's
    # in-memory cache is empty (fresh process) yet contains() must still return True.
    redis = _FakeRedis()
    await DurableDeduplicator(redis, MarketIpcConfig()).record(_identity())  # type: ignore[arg-type]

    fresh = _composite(redis)
    assert await fresh.contains(_identity()) is True


async def test_composite_record_writes_durable_before_cache() -> None:
    redis = _FakeRedis()
    composite = _composite(redis)
    await composite.record(_identity())
    assert dedup_key("md:dedup", _identity()) in redis.store  # durable authority written


async def test_composite_record_failure_does_not_pollute_cache() -> None:
    redis = _FakeRedis(fail=True)
    composite = _composite(redis)
    with pytest.raises(RedisError):
        await composite.record(_identity())
    # durable failed first -> cache not warmed, so a later (healthy) contains still says False.
    redis._fail = False
    assert await composite.contains(_identity()) is False


async def test_composite_contains_warms_cache_from_durable_hit() -> None:
    redis = _FakeRedis()
    await DurableDeduplicator(redis, MarketIpcConfig()).record(_identity())  # type: ignore[arg-type]
    composite = _composite(redis)

    assert await composite.contains(_identity()) is True  # durable hit warms the cache
    redis._fail = True  # durable now unavailable
    assert await composite.contains(_identity()) is True  # served from the warmed cache
