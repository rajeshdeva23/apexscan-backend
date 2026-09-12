"""Durable cross-process consumer idempotency for market IPC (DECOUPLING PHASE C1).

Phase C deduplicated by ``(producer_id, producer_epoch, producer_sequence)`` in a bounded
in-memory window, so a process/consumer restart re-applied already-processed events. C1 adds a
**durable** dedup authority (a Redis key per canonical identity, TTL-bounded) so a completed
application is recognised across process restart, consumer restart, Redis Stream redelivery,
ACK failure, and different transport (Redis Stream) IDs carrying the same canonical identity.

Correctness comes from the durable store; the in-memory window is only an optional hot-path
cache. The dedup store failing is **fail-closed** at the call site (the exception propagates so
the consumer leaves the entry pending rather than applying it without idempotency protection).

Scope: this is a durable **idempotent-consumer** mechanism, not exactly-once delivery. The
producer/stream remain at-least-once; the residual "apply succeeded but the durable mark had not
yet committed" window can re-apply an event on redelivery — harmless for the current
non-authoritative shadow sink (no durable/business side effect). Coupling the durable mark
atomically with a future *authoritative* sink's own state transition is deferred to when that
sink is connected (see ADR-023).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from redis.asyncio import Redis

from app.market_ipc.config import MarketIpcConfig
from app.market_ipc.dedup import BoundedDeduplicator
from app.market_ipc.envelope import ProducerEventIdentity


def dedup_key(prefix: str, identity: ProducerEventIdentity) -> str:
    """Collision-free Redis key for a canonical identity (length-prefixed producer_id).

    The producer_id length is encoded so no two distinct ``(producer_id, epoch, sequence)``
    triples can ever map to the same key regardless of the producer_id's contents (e.g. if it
    ever contained a delimiter). Redis Stream (transport) IDs are deliberately NOT part of the
    key — the canonical identity is the dedup identity.
    """
    return (
        f"{prefix}:{len(identity.producer_id)}:{identity.producer_id}"
        f":{identity.producer_epoch}:{identity.producer_sequence}"
    )


@runtime_checkable
class Deduplicator(Protocol):
    """Async dedup authority over canonical producer event identities."""

    async def contains(self, identity: ProducerEventIdentity) -> bool:
        """Whether ``identity`` was already durably recorded as applied."""
        ...

    async def record(self, identity: ProducerEventIdentity) -> None:
        """Durably record ``identity`` as applied (idempotent)."""
        ...


class InMemoryDeduplicator:
    """Async adapter over the bounded in-memory window (NON-durable; default / tests only).

    Preserves Phase-C behaviour when no durable store is wired: correctness holds only within a
    single process lifetime. Use :class:`CompositeDeduplicator` for durable C1 semantics.
    """

    def __init__(self, delegate: BoundedDeduplicator) -> None:
        self._delegate = delegate

    async def contains(self, identity: ProducerEventIdentity) -> bool:
        """Read-only membership test in the in-memory window."""
        return self._delegate.contains(identity)

    async def record(self, identity: ProducerEventIdentity) -> None:
        """Record the identity in the in-memory window."""
        self._delegate.record(identity)


class DurableDeduplicator:
    """Redis-backed durable dedup authority: one TTL-bounded key per canonical identity.

    ``contains`` is a durable ``EXISTS`` (survives process/consumer restart); ``record`` is a
    ``SET`` with a bounded TTL so storage cannot grow without limit. Redis errors are NOT
    swallowed — they propagate so the caller fails closed (never applies an event without
    idempotency protection). Durability is exactly the configured Redis persistence: a total
    Redis data/volume loss discards both the stream and this dedup state together (documented).
    """

    def __init__(self, redis: Redis, config: MarketIpcConfig) -> None:
        self._redis = redis
        self._prefix = config.dedup_key_prefix
        self._ttl_seconds = config.dedup_ttl_seconds

    async def contains(self, identity: ProducerEventIdentity) -> bool:
        """Durable membership test; raises RedisError on store failure (caller fails closed)."""
        return bool(await self._redis.exists(dedup_key(self._prefix, identity)))

    async def record(self, identity: ProducerEventIdentity) -> None:
        """Durably mark ``identity`` applied with the bounded dedup TTL (idempotent SET)."""
        await self._redis.set(dedup_key(self._prefix, identity), b"1", ex=self._ttl_seconds)


class CompositeDeduplicator:
    """Durable authority (Redis) fronted by a bounded in-memory hot cache.

    Correctness comes solely from the durable store: a fresh process (empty cache) still sees a
    previously-recorded identity via the durable ``contains``. The cache only short-circuits
    repeat lookups within one process. ``record`` writes the durable store FIRST — if that
    raises, the cache is not polluted and the caller fails closed.
    """

    def __init__(self, *, memory: BoundedDeduplicator, durable: DurableDeduplicator) -> None:
        self._memory = memory
        self._durable = durable

    async def contains(self, identity: ProducerEventIdentity) -> bool:
        """True if cached, else consult the durable authority (and warm the cache on a hit)."""
        if self._memory.contains(identity):
            return True
        if await self._durable.contains(identity):
            self._memory.record(identity)  # warm the cache from the durable authority
            return True
        return False

    async def record(self, identity: ProducerEventIdentity) -> None:
        """Record durably (authority) then cache; a durable failure propagates before caching."""
        await self._durable.record(identity)
        self._memory.record(identity)
