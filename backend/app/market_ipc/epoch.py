"""Producer-epoch allocation for IPC dedup identity (DECOUPLING PHASE B).

Dedup identity is ``(producer_id, producer_epoch, producer_sequence)``. ``producer_sequence``
resets on every producer restart, so ``producer_epoch`` MUST change on each restart and must
never be reused for a given ``producer_id`` — otherwise a post-restart ``seq=1`` collides with
the previous run's ``seq=1``.

The allocator uses a Redis ``INCR`` on a per-producer key. ``INCR`` is atomic, so a restart or
an accidental concurrent startup each receive a distinct, monotonically increasing epoch. If
Redis is unreachable, allocation raises — the caller must fail the publisher activation safely
rather than fabricate or reuse an epoch (the authoritative in-process path is unaffected).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from redis.asyncio import Redis

EPOCH_KEY_PREFIX = "md:producer:epoch"


@runtime_checkable
class EpochAllocator(Protocol):
    """Allocates a restart-unique, monotonic epoch for a logical producer."""

    async def allocate(self, producer_id: str) -> int:
        """Return a new epoch for ``producer_id`` that was never returned before."""
        ...


class RedisEpochAllocator:
    """Redis ``INCR``-backed epoch allocator (atomic, restart- and concurrency-safe)."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def allocate(self, producer_id: str) -> int:
        """Atomically increment and return the producer's epoch counter.

        Raises:
            redis.exceptions.RedisError: If Redis is unreachable; the caller must not
                fabricate or reuse an epoch.
        """
        if not producer_id:
            raise ValueError("producer_id must be non-empty")
        return int(await self._redis.incr(f"{EPOCH_KEY_PREFIX}:{producer_id}"))
