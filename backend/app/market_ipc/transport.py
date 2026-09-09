"""Redis Streams transport abstraction for IPC events (PHASE A).

Defines the :class:`MarketEventStream` interface the future producer/consumer use, a
Redis-backed implementation, and an in-memory reference implementation for tests. Nothing
here is constructed by application composition, so merging Phase A publishes no live events.

No-silent-fallback: a failed publish raises :class:`RedisPublishError` and never returns a
message id, so a transport failure can never be mistaken for a successful publication (the
Phase B in-memory failure ring builds on this contract).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.market_ipc.config import MarketIpcConfig
from app.market_ipc.envelope import MarketEventEnvelope, decode_envelope, encode_envelope

_FIELD = "e"  # single stream field carrying the encoded envelope


class RedisPublishError(RuntimeError):
    """Raised when an event could not be durably published; never a silent success."""


DeliveredEvent = tuple[str, MarketEventEnvelope]


@runtime_checkable
class MarketEventStream(Protocol):
    """At-least-once event stream over a single ordered channel (``md:events``)."""

    async def ensure_group(self) -> None:
        """Create the consumer group (and stream) idempotently."""
        ...

    async def publish(self, envelope: MarketEventEnvelope) -> str:
        """Append one event and return its message id, or raise on failure."""
        ...

    async def read(self) -> list[DeliveredEvent]:
        """Read a batch of newly delivered events for this consumer."""
        ...

    async def ack(self, *message_ids: str) -> int:
        """Acknowledge processed events; return the count acknowledged."""
        ...

    async def claim_stale(self) -> list[DeliveredEvent]:
        """Redeliver events idle past the configured threshold."""
        ...


class RedisMarketEventStream:
    """Redis Streams implementation of :class:`MarketEventStream` (not composed in Phase A)."""

    def __init__(self, redis: Redis, config: MarketIpcConfig) -> None:
        self._redis = redis
        self._config = config

    async def ensure_group(self) -> None:
        """Create the consumer group (and stream) idempotently."""
        try:
            await self._redis.xgroup_create(
                self._config.stream_name, self._config.consumer_group, id="0", mkstream=True
            )
        except RedisError as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def publish(self, envelope: MarketEventEnvelope) -> str:
        """XADD one event with an approximate MAXLEN cap; raise on any transport failure."""
        raw = encode_envelope(envelope, max_bytes=self._config.max_payload_bytes)
        try:
            message_id = await self._redis.xadd(
                self._config.stream_name,
                {_FIELD: raw},
                maxlen=self._config.maxlen,
                approximate=True,
            )
        except RedisError as error:
            raise RedisPublishError(f"failed to publish to {self._config.stream_name}") from error
        return message_id.decode() if isinstance(message_id, bytes) else str(message_id)

    async def read(self) -> list[DeliveredEvent]:
        """XREADGROUP a batch of new events for this consumer."""
        response = await self._redis.xreadgroup(
            self._config.consumer_group,
            self._config.consumer_name,
            {self._config.stream_name: ">"},
            count=self._config.read_count,
            block=self._config.block_ms,
        )
        return _decode_stream_response(response)

    async def ack(self, *message_ids: str) -> int:
        """XACK processed events; returns the count acknowledged."""
        if not message_ids:
            return 0
        return await self._redis.xack(
            self._config.stream_name, self._config.consumer_group, *message_ids
        )

    async def claim_stale(self) -> list[DeliveredEvent]:
        """XAUTOCLAIM events idle past the configured threshold (redelivery).

        XAUTOCLAIM returns ``(cursor, messages)`` on Redis 6.2 and ``(cursor, messages,
        deleted)`` on Redis >= 7.0; index the messages positionally to support both.
        """
        response = await self._redis.xautoclaim(
            self._config.stream_name,
            self._config.consumer_group,
            self._config.consumer_name,
            min_idle_time=self._config.claim_idle_ms,
            start_id="0-0",
            count=self._config.read_count,
        )
        messages = response[1]
        return [_decode_entry(message_id, fields) for message_id, fields in messages]


def _decode_stream_response(response: Any) -> list[DeliveredEvent]:
    """Decode the loosely-typed XREADGROUP/redis payload into typed delivered events."""
    events: list[DeliveredEvent] = []
    for _stream, entries in response or []:
        for message_id, fields in entries:
            events.append(_decode_entry(message_id, fields))
    return events


def _decode_entry(message_id: Any, fields: dict[Any, Any]) -> DeliveredEvent:
    raw = fields.get(_FIELD) or fields.get(_FIELD.encode())
    if raw is None:
        raise ValueError(f"stream entry {message_id!r} missing field {_FIELD!r}")
    message = message_id.decode() if isinstance(message_id, bytes) else str(message_id)
    return message, decode_envelope(raw)


class InMemoryMarketEventStream:
    """Reference/test stream: single-consumer FIFO with a pending (unacked) set."""

    def __init__(self) -> None:
        self._entries: list[DeliveredEvent] = []
        self._cursor = 0
        self._pending: dict[str, MarketEventEnvelope] = {}

    async def ensure_group(self) -> None:
        """No-op: the in-memory stream has an implicit single group."""
        return None

    async def publish(self, envelope: MarketEventEnvelope) -> str:
        """Append the envelope and return its monotonic message id."""
        message_id = str(len(self._entries))
        self._entries.append((message_id, envelope))
        return message_id

    async def read(self) -> list[DeliveredEvent]:
        """Return events after the cursor and mark them pending (unacked)."""
        batch = self._entries[self._cursor :]
        self._cursor = len(self._entries)
        for message_id, envelope in batch:
            self._pending[message_id] = envelope
        return batch

    async def ack(self, *message_ids: str) -> int:
        """Drop the given ids from the pending set; return the count removed."""
        return sum(self._pending.pop(mid, None) is not None for mid in message_ids)

    async def claim_stale(self) -> list[DeliveredEvent]:
        """Return all currently pending (unacked) events."""
        return list(self._pending.items())
