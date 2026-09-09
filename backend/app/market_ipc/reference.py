"""Compacted reference-state recovery: writer + loader over Redis (DECOUPLING PHASE D).

Backend restart must recover correctness-critical session reference data (``previous_close``,
session OHLC) without Dhan re-auth / WS reconnect / code-6 re-delivery. This module stores the
last-known canonical reference per instrument in a Redis hash ``md:reference:<trading_date>``
and reloads it on restart.

Authorities are unchanged and never fabricated: ``previous_close`` comes only from canonical
``MarketReference`` (Dhan code-6), ``session_open`` only from ``Tick.session_ohlc.open_price``
(raw WS session OHLC). A field is stored only after its canonical value arrived.

Compaction is monotonic and non-destructive: an update applies only when its ``(producer_epoch,
producer_sequence)`` is newer, merges price fields without clearing ones it lacks, and is written
atomically (Redis WATCH/MULTI) so a concurrent update never loses a write. Trading-date isolation
(one key per date) independently prevents cross-day leakage; TTL is retention only, not
correctness. Everything here is off by default and not composed into production.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.market_ipc.config import MarketIpcConfig
from app.market_ipc.envelope import (
    MarketEventEnvelope,
    UniverseVersionComparison,
    compare_universe_version,
)
from app.market_ipc.events import decode_payload
from app.market_ipc.state import CompactedReferenceState, reference_key
from app.schemas.market_data import MarketReference, Tick

if TYPE_CHECKING:
    from redis.commands.core import AsyncScript

# Server-side atomic monotonic compaction (mirrors _classify + merge_reference). Running the
# read-order-merge-write inside one Lua call guarantees newest-wins under concurrent same-field
# updates with no lost update and no client retry (WATCH/MULTI would retry-exhaust under load).
# Safe round-trip: pydantic serialises Decimal as a JSON string and keeps None fields as null, so
# cjson.decode/encode never touches numeric precision.
_COMPACT_LUA = """
local existing = redis.call('HGET', KEYS[1], ARGV[1])
local inc_epoch = tonumber(ARGV[2])
local inc_seq = tonumber(ARGV[3])
local ttl = tonumber(ARGV[5])
local status
if existing then
  local ex = cjson.decode(existing)
  local ex_epoch = tonumber(ex.producer_epoch)
  local ex_seq = tonumber(ex.producer_sequence)
  if inc_epoch < ex_epoch or (inc_epoch == ex_epoch and inc_seq < ex_seq) then
    return 'stale_rejected'
  end
  if inc_epoch == ex_epoch and inc_seq == ex_seq then
    return 'duplicate'
  end
  local inc = cjson.decode(ARGV[4])
  local price_fields = {
    'previous_close', 'session_open', 'session_high', 'session_low', 'session_close'
  }
  for _, f in ipairs(price_fields) do
    if inc[f] == nil or inc[f] == cjson.null then
      inc[f] = ex[f]
    end
  end
  redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(inc))
  status = 'merged'
else
  redis.call('HSET', KEYS[1], ARGV[1], ARGV[4])
  status = 'written'
end
redis.call('EXPIRE', KEYS[1], ttl)
return status
"""


class ReferenceOutcome(StrEnum):
    """Deterministic result of one reference compaction attempt."""

    WRITTEN = "written"  # first state for this instrument on this date
    MERGED = "merged"  # newer state merged non-destructively into existing
    DUPLICATE = "duplicate"  # same ordering identity already stored
    STALE_REJECTED = "stale_rejected"  # older ordering than stored; not applied
    NO_REFERENCE_DATA = "no_reference_data"  # event carries nothing to compact
    WRITE_FAILED = "write_failed"  # Redis/transport failure or retry exhaustion


def reference_from_envelope(envelope: MarketEventEnvelope) -> CompactedReferenceState | None:
    """Extract a partial compacted reference from a canonical envelope, or None if irrelevant.

    Only ``MarketReference`` (previous_close) and ``Tick.session_ohlc`` (session OHLC) carry
    reference data; every other kind returns None. No value is derived or fabricated.
    """
    payload = decode_payload(envelope.event_kind, envelope.payload)
    fields: dict[str, object] = {}
    if isinstance(payload, MarketReference):
        fields["previous_close"] = payload.previous_close
    elif isinstance(payload, Tick) and payload.session_ohlc is not None:
        ohlc = payload.session_ohlc
        fields["session_open"] = ohlc.open_price
        fields["session_high"] = ohlc.high_price
        fields["session_low"] = ohlc.low_price
        fields["session_close"] = ohlc.close_price
    else:
        return None
    return CompactedReferenceState(
        instrument_identity=envelope.instrument_identity,
        trading_date=envelope.trading_date,
        updated_at=envelope.produced_at,
        universe_version=envelope.universe_version,
        producer_id=envelope.producer_id,
        producer_epoch=envelope.producer_epoch,
        producer_sequence=envelope.producer_sequence,
        **fields,  # type: ignore[arg-type]  # Decimal values keyed to optional price fields
    )


def merge_reference(
    existing: CompactedReferenceState, incoming: CompactedReferenceState
) -> CompactedReferenceState:
    """Merge ``incoming`` (newer) onto ``existing`` non-destructively.

    A price field the incoming state lacks keeps the existing value (a later Tick never clears
    ``previous_close``; a later MarketReference never clears ``session_open``). Provenance,
    ``updated_at`` and ``universe_version`` become the incoming (newer) state's.
    """
    return incoming.model_copy(
        update={
            "previous_close": incoming.previous_close
            if incoming.previous_close is not None
            else existing.previous_close,
            "session_open": incoming.session_open
            if incoming.session_open is not None
            else existing.session_open,
            "session_high": incoming.session_high
            if incoming.session_high is not None
            else existing.session_high,
            "session_low": incoming.session_low
            if incoming.session_low is not None
            else existing.session_low,
            "session_close": incoming.session_close
            if incoming.session_close is not None
            else existing.session_close,
        }
    )


@runtime_checkable
class ReferenceEntrySource(Protocol):
    """Raw per-instrument reference entries for one trading date (loader dependency)."""

    async def read_all_raw(self, trading_date: date) -> dict[str, bytes]:
        """Return ``instrument_identity -> serialized state`` for one trading date."""
        ...


class RedisCompactedReferenceStore:
    """Redis-hash compacted reference store: atomic monotonic compaction + raw recovery reads.

    One hash per trading date (``md:reference:<date>``), field = broker-neutral
    ``instrument_identity``, value = ``CompactedReferenceState`` JSON. No blind ``put``: all
    writes go through :meth:`compact` so ordering/merge invariants always hold.
    """

    def __init__(self, redis: Redis, config: MarketIpcConfig) -> None:
        self._redis = redis
        self._config = config
        self._compact_script: AsyncScript = redis.register_script(_COMPACT_LUA)

    async def compact(self, incoming: CompactedReferenceState) -> ReferenceOutcome:
        """Atomically merge ``incoming`` into the stored state via a single Lua call.

        The Lua script reads the field, rejects an older ordering (STALE) or an equal one
        (DUPLICATE), otherwise merges price fields non-destructively and writes — all atomically,
        so concurrent same-field updates deterministically converge on the newest with no lost
        update and no client retry. Refreshes the key TTL.
        """
        key = reference_key(self._config.reference_key_prefix, incoming.trading_date)
        result = await self._compact_script(
            keys=[key],
            args=[
                incoming.instrument_identity,
                incoming.producer_epoch,
                incoming.producer_sequence,
                incoming.model_dump_json(),
                self._config.reference_ttl_seconds,
            ],
        )
        return ReferenceOutcome(result.decode() if isinstance(result, bytes) else str(result))

    async def read_all_raw(self, trading_date: date) -> dict[str, bytes]:
        """HGETALL the trading-date hash as ``instrument_identity -> raw bytes`` (may be empty)."""
        raw: dict[bytes | str, bytes | str] = await self._redis.hgetall(
            reference_key(self._config.reference_key_prefix, trading_date)
        )
        return {_to_str(k): _to_bytes(v) for k, v in raw.items()}

    async def get(
        self, trading_date: date, instrument_identity: str
    ) -> CompactedReferenceState | None:
        """Return one instrument's stored reference, or None."""
        raw = await self._redis.hget(
            reference_key(self._config.reference_key_prefix, trading_date), instrument_identity
        )
        return _safe_load(_to_bytes(raw) if raw is not None else None)

    async def all(self, trading_date: date) -> tuple[CompactedReferenceState, ...]:
        """Return every well-formed stored reference for one trading date."""
        entries = await self.read_all_raw(trading_date)
        return tuple(state for state in (_safe_load(v) for v in entries.values()) if state)


def _classify(
    incoming: CompactedReferenceState, existing: CompactedReferenceState | None
) -> ReferenceOutcome:
    """Ordering verdict for an incoming state against the existing one."""
    if existing is None:
        return ReferenceOutcome.WRITTEN
    if incoming.ordering < existing.ordering:
        return ReferenceOutcome.STALE_REJECTED
    if incoming.ordering == existing.ordering:
        return ReferenceOutcome.DUPLICATE
    return ReferenceOutcome.MERGED


class WriterDiagnostics(BaseModel):
    """Bounded, credential-free reference-writer counters."""

    model_config = ConfigDict(frozen=True)

    reference_updates_attempted: int
    reference_updates_written: int
    reference_update_failures: int
    previous_close_updates: int
    session_open_updates: int
    stale_updates_rejected: int
    duplicate_updates: int
    no_reference_data: int
    last_success_at: datetime | None
    last_failure_at: datetime | None


class ReferenceStateWriter:
    """Compacts canonical envelopes into Redis reference state; off-by-default, failure-isolated."""

    def __init__(
        self,
        *,
        store: RedisCompactedReferenceStore,
        now: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._now = now
        self._counters = _WriterCounters()
        self._last_success_at: datetime | None = None
        self._last_failure_at: datetime | None = None

    async def update(self, envelope: MarketEventEnvelope) -> ReferenceOutcome:
        """Extract and atomically compact one envelope's reference data; never raises."""
        self._counters.attempted += 1
        try:
            incoming = reference_from_envelope(envelope)
        except (ValidationError, ValueError, KeyError):
            return self._fail()
        if incoming is None:
            self._counters.no_reference_data += 1
            return ReferenceOutcome.NO_REFERENCE_DATA
        try:
            outcome = await self._store.compact(incoming)
        except RedisError:
            return self._fail()
        return self._record(outcome, incoming)

    def _record(
        self, outcome: ReferenceOutcome, incoming: CompactedReferenceState
    ) -> ReferenceOutcome:
        """Update counters for a completed compaction and return the outcome."""
        if outcome in (ReferenceOutcome.WRITTEN, ReferenceOutcome.MERGED):
            self._counters.written += 1
            if incoming.previous_close is not None:
                self._counters.previous_close += 1
            if incoming.session_open is not None:
                self._counters.session_open += 1
            self._last_success_at = self._now()
        elif outcome is ReferenceOutcome.STALE_REJECTED:
            self._counters.stale += 1
        elif outcome is ReferenceOutcome.DUPLICATE:
            self._counters.duplicate += 1
        elif outcome is ReferenceOutcome.WRITE_FAILED:
            return self._fail()
        return outcome

    def _fail(self) -> ReferenceOutcome:
        """Count a write failure and stamp the failure time."""
        self._counters.failures += 1
        self._last_failure_at = self._now()
        return ReferenceOutcome.WRITE_FAILED

    def diagnostics(self) -> WriterDiagnostics:
        """Snapshot the bounded writer counters."""
        counters = self._counters
        return WriterDiagnostics(
            reference_updates_attempted=counters.attempted,
            reference_updates_written=counters.written,
            reference_update_failures=counters.failures,
            previous_close_updates=counters.previous_close,
            session_open_updates=counters.session_open,
            stale_updates_rejected=counters.stale,
            duplicate_updates=counters.duplicate,
            no_reference_data=counters.no_reference_data,
            last_success_at=self._last_success_at,
            last_failure_at=self._last_failure_at,
        )


class LoaderDiagnostics(BaseModel):
    """Bounded, credential-free reference-loader counters for one load."""

    model_config = ConfigDict(frozen=True)

    entries_total: int
    entries_loaded: int
    entries_invalid: int
    previous_close_count: int
    session_open_count: int
    complete_reference_count: int
    universe_mismatch_count: int
    trading_date_mismatch_count: int
    last_load_at: datetime


class ReferenceSnapshot(BaseModel):
    """Deterministic recovery snapshot for one trading date (non-authoritative shadow state)."""

    model_config = ConfigDict(frozen=True)

    trading_date: date
    states: dict[str, CompactedReferenceState]
    diagnostics: LoaderDiagnostics

    @property
    def warming_up(self) -> bool:
        """Whether no reference state has been recovered yet (empty/pre-ingestion)."""
        return not self.states


class ReferenceStateLoader:
    """Loads compacted reference state on backend restart; validates and isolates per entry."""

    def __init__(self, *, source: ReferenceEntrySource, now: Callable[[], datetime]) -> None:
        self._source = source
        self._now = now

    async def load(
        self, trading_date: date, expected_universe_version: int | None
    ) -> ReferenceSnapshot:
        """Load, validate and gate the trading-date hash into a deterministic snapshot.

        Reads only ``md:reference:<trading_date>``; skips malformed/mismatched entries
        individually (never mixing another day's state); returns an empty snapshot when the hash
        is empty (warming up). Redis unavailability propagates as ``RedisError`` so a caller can
        never mistake a transport outage for an empty session.
        """
        raw_entries = await self._source.read_all_raw(trading_date)
        states: dict[str, CompactedReferenceState] = {}
        tally = _LoaderTally()
        for value in raw_entries.values():
            tally.total += 1
            state = _safe_load(value)
            if state is None:
                tally.invalid += 1
                continue
            if state.trading_date != trading_date:
                tally.trading_date_mismatch += 1
                continue
            if compare_universe_version(state.universe_version, expected_universe_version) is not (
                UniverseVersionComparison.MATCH
            ):
                tally.universe_mismatch += 1
                continue
            states[state.instrument_identity] = state
            tally.loaded += 1
            if state.previous_close is not None:
                tally.previous_close += 1
            if state.session_open is not None:
                tally.session_open += 1
            if state.previous_close is not None and state.session_open is not None:
                tally.complete += 1
        return ReferenceSnapshot(
            trading_date=trading_date, states=states, diagnostics=tally.finish(self._now())
        )


def _safe_load(raw: bytes | str | None) -> CompactedReferenceState | None:
    """Deserialize one reference entry, returning None on any malformed value."""
    if raw is None:
        return None
    try:
        return CompactedReferenceState.model_validate_json(raw)
    except ValidationError:
        return None


def _to_str(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _to_bytes(value: bytes | str) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")


class _WriterCounters:
    """Mutable bounded writer counters (fixed field set)."""

    __slots__ = (
        "attempted",
        "written",
        "failures",
        "previous_close",
        "session_open",
        "stale",
        "duplicate",
        "no_reference_data",
    )

    def __init__(self) -> None:
        self.attempted = 0
        self.written = 0
        self.failures = 0
        self.previous_close = 0
        self.session_open = 0
        self.stale = 0
        self.duplicate = 0
        self.no_reference_data = 0


class _LoaderTally:
    """Mutable bounded loader counters for one load call."""

    __slots__ = (
        "total",
        "loaded",
        "invalid",
        "previous_close",
        "session_open",
        "complete",
        "universe_mismatch",
        "trading_date_mismatch",
    )

    def __init__(self) -> None:
        self.total = 0
        self.loaded = 0
        self.invalid = 0
        self.previous_close = 0
        self.session_open = 0
        self.complete = 0
        self.universe_mismatch = 0
        self.trading_date_mismatch = 0

    def finish(self, load_at: datetime) -> LoaderDiagnostics:
        """Freeze the tally into loader diagnostics stamped at ``load_at``."""
        return LoaderDiagnostics(
            entries_total=self.total,
            entries_loaded=self.loaded,
            entries_invalid=self.invalid,
            previous_close_count=self.previous_close,
            session_open_count=self.session_open,
            complete_reference_count=self.complete,
            universe_mismatch_count=self.universe_mismatch,
            trading_date_mismatch_count=self.trading_date_mismatch,
            last_load_at=load_at,
        )
