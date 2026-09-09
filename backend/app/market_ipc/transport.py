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
_CURSOR_START = "0-0"  # XAUTOCLAIM start cursor; a returned "0-0" means the scan is complete


class RedisPublishError(RuntimeError):
    """Raised when an event could not be durably published; never a silent success."""


DeliveredEvent = tuple[str, MarketEventEnvelope]

# (message_id, raw envelope bytes) or (message_id, None) when the entry is missing its field.
# The consumer decodes raw bytes itself so one malformed entry can be classified as poison
# without failing the decode of the whole batch (which the pre-decoded API cannot do).
RawDeliveredEvent = tuple[str, bytes | None]
RawClaimPage = tuple[str, list[RawDeliveredEvent]]  # (next cursor, entries)


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

    async def read_raw(self) -> list[RawDeliveredEvent]:
        """Read a batch of newly delivered events as raw bytes (consumer decodes them)."""
        ...

    async def ack(self, *message_ids: str) -> int:
        """Acknowledge processed events; return the count acknowledged."""
        ...

    async def claim_stale(self) -> list[DeliveredEvent]:
        """Redeliver events idle past the configured threshold."""
        ...

    async def claim_page_raw(self, start_id: str) -> RawClaimPage:
        """Claim one bounded page of stale pending entries as raw bytes; return next cursor."""
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
            block=self._block_arg(),
        )
        return _decode_stream_response(response)

    def _block_arg(self) -> int | None:
        """BLOCK argument for XREADGROUP: ``None`` (non-blocking) when ``block_ms`` is 0.

        Redis treats ``BLOCK 0`` as *block forever*; omitting BLOCK returns immediately with
        whatever is available. ``block_ms == 0`` therefore selects a non-blocking poll (used by
        the bounded consumer cycle), and any positive value long-polls for that many ms.
        """
        return self._config.block_ms or None

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

    async def read_raw(self) -> list[RawDeliveredEvent]:
        """XREADGROUP a batch of new entries, returning raw envelope bytes for the consumer."""
        response: Any = await self._redis.xreadgroup(
            self._config.consumer_group,
            self._config.consumer_name,
            {self._config.stream_name: ">"},
            count=self._config.read_count,
            block=self._block_arg(),
        )
        entries: list[RawDeliveredEvent] = []
        for _stream, stream_entries in response or []:
            for message_id, fields in stream_entries:
                entries.append(_extract_raw(message_id, fields))
        return entries

    async def claim_page_raw(self, start_id: str) -> RawClaimPage:
        """XAUTOCLAIM one bounded page (``read_count``) of stale pending entries as raw bytes.

        Returns the next cursor to resume from; a cursor of ``"0-0"`` means the pending scan is
        complete. Paging (rather than one pass) lets a bounded consumer cycle recover a backlog
        larger than ``read_count`` across successive cycles without unbounded single-cycle work.
        """
        response: Any = await self._redis.xautoclaim(
            self._config.stream_name,
            self._config.consumer_group,
            self._config.consumer_name,
            min_idle_time=self._config.claim_idle_ms,
            start_id=start_id,
            count=self._config.read_count,
        )
        cursor = response[0]
        next_cursor = cursor.decode() if isinstance(cursor, bytes) else str(cursor)
        entries: list[RawDeliveredEvent] = [
            _extract_raw(message_id, fields) for message_id, fields in response[1]
        ]
        return next_cursor, entries


def _extract_raw(message_id: Any, fields: dict[Any, Any]) -> RawDeliveredEvent:
    """Extract ``(message_id, raw envelope bytes)`` without decoding; None if field is absent."""
    message = message_id.decode() if isinstance(message_id, bytes) else str(message_id)
    raw = fields.get(_FIELD) or fields.get(_FIELD.encode())
    if raw is None:
        return message, None
    return message, raw if isinstance(raw, bytes) else str(raw).encode("utf-8")


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

    async def read_raw(self) -> list[RawDeliveredEvent]:
        """Return new events as raw bytes and mark them pending (unacked)."""
        return [(mid, encode_envelope(env)) for mid, env in await self.read()]

    async def ack(self, *message_ids: str) -> int:
        """Drop the given ids from the pending set; return the count removed."""
        return sum(self._pending.pop(mid, None) is not None for mid in message_ids)

    async def claim_stale(self) -> list[DeliveredEvent]:
        """Return all currently pending (unacked) events."""
        return list(self._pending.items())

    async def claim_page_raw(self, start_id: str) -> RawClaimPage:
        """Return all pending events as one raw page (in-memory needs no cursor paging)."""
        entries: list[RawDeliveredEvent] = [
            (mid, encode_envelope(env)) for mid, env in self._pending.items()
        ]
        return _CURSOR_START, entries
