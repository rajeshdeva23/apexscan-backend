"""Atomic canonical-stream + compacted-reference publication (DECOUPLING PHASE D1).

Publishing a reference-bearing event needs two Redis effects — a canonical ``XADD md:events``
append and a compacted ``md:reference:<trading_date>`` transition. Phase B/D did these as two
separate round-trips, leaving a partial-write window: a crash between them could leave a stream
event with no reference projection, or a reference update for an event never appended to the
canonical stream (ADR-021).

D1 performs both effects in a single server-side Lua call, so for a STREAM_PLUS_REFERENCE event
another client can never observe one half without the other. A STREAM_ONLY event (no reference
data) uses the plain stream append and never touches a reference key or its TTL.

Critical Lua rule: Redis does NOT roll back writes already performed if a script errors later, so
every fallible check (argument parsing, JSON decode of existing/incoming state) runs BEFORE the
first write. After the first write the script issues only deterministic, already-validated
commands. The reference transition mirrors Phase D exactly (monotonic ``(producer_epoch,
producer_sequence)`` ordering, non-destructive price merge, TTL refresh on write/merge only).
This targets single-instance Redis (both keys on one node); Redis Cluster cross-slot is not
claimed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.market_ipc.config import MarketIpcConfig
from app.market_ipc.envelope import MarketEventEnvelope, encode_envelope
from app.market_ipc.reference import ReferenceOutcome
from app.market_ipc.state import CompactedReferenceState, reference_key
from app.market_ipc.transport import _FIELD, RedisPublishError

if TYPE_CHECKING:
    from redis.commands.core import AsyncScript

# KEYS[1]=stream, KEYS[2]=reference hash.
# ARGV: 1=stream_field, 2=raw_envelope, 3=maxlen, 4=instrument_identity,
#       5=inc_epoch, 6=inc_seq, 7=inc_state_json, 8=ttl_seconds
#
# All parsing/validation happens before the first write (XADD); after it, only deterministic
# commands run — so the script can never error after a partial effect.
_PUBLISH_STREAM_AND_REFERENCE_LUA = """
local maxlen = tonumber(ARGV[3])
local inc_epoch = tonumber(ARGV[5])
local inc_seq = tonumber(ARGV[6])
local ttl = tonumber(ARGV[8])
if maxlen == nil or inc_epoch == nil or inc_seq == nil or ttl == nil then
  return redis.error_reply('D1_INVALID_ARGS')
end
local existing = redis.call('HGET', KEYS[2], ARGV[4])
local ref_status
local to_write = nil
if existing then
  local ex = cjson.decode(existing)
  local ex_epoch = tonumber(ex.producer_epoch)
  local ex_seq = tonumber(ex.producer_sequence)
  if inc_epoch < ex_epoch or (inc_epoch == ex_epoch and inc_seq < ex_seq) then
    ref_status = 'stale_rejected'
  elseif inc_epoch == ex_epoch and inc_seq == ex_seq then
    ref_status = 'duplicate'
  else
    local inc = cjson.decode(ARGV[7])
    local price_fields = {
      'previous_close', 'session_open', 'session_high', 'session_low', 'session_close'
    }
    for _, f in ipairs(price_fields) do
      if inc[f] == nil or inc[f] == cjson.null then
        inc[f] = ex[f]
      end
    end
    to_write = cjson.encode(inc)
    ref_status = 'merged'
  end
else
  cjson.decode(ARGV[7])  -- validate incoming JSON BEFORE any write; errors here are pre-write
  to_write = ARGV[7]
  ref_status = 'written'
end
-- First write onwards: only deterministic, already-validated commands.
local mid = redis.call('XADD', KEYS[1], 'MAXLEN', '~', maxlen, '*', ARGV[1], ARGV[2])
if to_write ~= nil then
  redis.call('HSET', KEYS[2], ARGV[4], to_write)
  redis.call('EXPIRE', KEYS[2], ttl)
end
return {mid, ref_status}
"""


@dataclass(frozen=True, slots=True)
class AtomicPublicationResult:
    """The result of one atomic publication.

    Attributes:
        message_id: The stream message id of the (always-appended) canonical event.
        reference_outcome: How the reference transition resolved (written / merged / stale /
            duplicate), or ``NO_REFERENCE_DATA`` for a STREAM_ONLY publication.
    """

    message_id: str
    reference_outcome: ReferenceOutcome


@runtime_checkable
class AtomicPublisher(Protocol):
    """Publishes a canonical event and its reference transition atomically."""

    async def publish_stream_only(self, envelope: MarketEventEnvelope) -> AtomicPublicationResult:
        """Append a canonical event that carries no reference data (stream only)."""
        ...

    async def publish_stream_and_reference(
        self, envelope: MarketEventEnvelope, reference_state: CompactedReferenceState
    ) -> AtomicPublicationResult:
        """Append the canonical event AND apply its reference transition atomically."""
        ...


class RedisAtomicPublisher:
    """Redis Lua implementation of the atomic stream+reference publication (single instance)."""

    def __init__(self, redis: Redis, config: MarketIpcConfig) -> None:
        self._redis = redis
        self._config = config
        self._script: AsyncScript = redis.register_script(_PUBLISH_STREAM_AND_REFERENCE_LUA)

    async def publish_stream_only(self, envelope: MarketEventEnvelope) -> AtomicPublicationResult:
        """Plain ``XADD`` for a STREAM_ONLY event; never touches a reference key or its TTL."""
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
        return AtomicPublicationResult(_as_str(message_id), ReferenceOutcome.NO_REFERENCE_DATA)

    async def publish_stream_and_reference(
        self, envelope: MarketEventEnvelope, reference_state: CompactedReferenceState
    ) -> AtomicPublicationResult:
        """Append the event and apply its reference transition in one atomic Lua call.

        The stream is always appended (a canonical event that happened is never dropped merely
        because its compacted projection is stale/duplicate); the reference hash changes only
        when the incoming ordering is newer, and only ever within this single atomic call — so no
        client observes a stream/reference partial state. Raises :class:`RedisPublishError` on any
        transport failure (never a silent partial success).
        """
        raw = encode_envelope(envelope, max_bytes=self._config.max_payload_bytes)
        key_reference = reference_key(
            self._config.reference_key_prefix, reference_state.trading_date
        )
        try:
            result = await self._script(
                keys=[self._config.stream_name, key_reference],
                args=[
                    _FIELD,
                    raw,
                    self._config.maxlen,
                    reference_state.instrument_identity,
                    reference_state.producer_epoch,
                    reference_state.producer_sequence,
                    reference_state.model_dump_json(),
                    self._config.reference_ttl_seconds,
                ],
            )
        except RedisError as error:
            raise RedisPublishError(
                f"failed atomic publish to {self._config.stream_name}"
            ) from error
        message_id, status = result[0], result[1]
        return AtomicPublicationResult(_as_str(message_id), ReferenceOutcome(_as_str(status)))


def _as_str(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
