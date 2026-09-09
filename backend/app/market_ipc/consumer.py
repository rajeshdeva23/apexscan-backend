"""Backend-side SHADOW Redis Streams consumer for canonical IPC events (DECOUPLING PHASE C).

Reads :class:`MarketEventEnvelope` bytes from the Phase-A/B ``md:events`` stream, validates
them (schema version, trading date, universe version, event-kind/payload consistency),
deduplicates by ``(producer_id, producer_epoch, producer_sequence)``, decodes the canonical
payload, and hands the accepted event to a NON-AUTHORITATIVE :class:`ShadowMarketEventSink`.

This is SHADOW ONLY. It never drives the production TickEngine/EventBus, never publishes to
the production bus, never invokes strategies/scanner/sector, never mutates business state, and
is not constructed by production composition. The in-process Dhan pipeline stays authoritative.

Delivery is at-least-once (Redis redelivery via XAUTOCLAIM). Dedup is committed only AFTER a
successful shadow application, so a failed apply is redelivered and retried rather than lost.
Exactly-once is NOT claimed: dedup is a bounded in-memory window, so a process restart or total
Redis state loss can re-apply an event (see carried-forward finding M1).
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError
from redis.exceptions import RedisError

from app.market_ipc.config import MarketIpcConfig
from app.market_ipc.dedup import BoundedDeduplicator
from app.market_ipc.envelope import (
    SUPPORTED_SCHEMA_VERSIONS,
    MarketEventEnvelope,
    ProducerEventIdentity,
    UniverseVersionComparison,
    compare_universe_version,
    decode_envelope,
)
from app.market_ipc.events import EventKind, IpcPayload, decode_payload
from app.market_ipc.transport import _CURSOR_START, MarketEventStream, RawDeliveredEvent

# The consumer's current trading-date authority (in production: MarketSessionClassifier); the
# consumer never uses datetime.now().date() nor infers the date from Redis stream ids.
TradingDateAuthority = Callable[[], date | None]
# The consumer's current universe version (Phase-C provisional); ``None`` -> UNKNOWN. Accepts a
# bound ``UniverseVersionSource.current_universe_version`` (existing provisional abstraction).
UniverseVersionAuthority = Callable[[], int | None]


class MessageOutcome(StrEnum):
    """Deterministic terminal classification of one consumed entry."""

    APPLIED = "applied"
    DUPLICATE = "duplicate"
    STALE_TRADING_DATE = "stale_trading_date"
    FUTURE_TRADING_DATE = "future_trading_date"
    OLDER_UNIVERSE = "older_universe"
    NEWER_UNIVERSE = "newer_universe"
    UNKNOWN_UNIVERSE = "unknown_universe"
    ENVELOPE_DECODE_FAILED = "envelope_decode_failed"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    PAYLOAD_DECODE_FAILED = "payload_decode_failed"
    EVENT_KIND_MISMATCH = "event_kind_mismatch"
    SINK_FAILED = "sink_failed"
    ACK_FAILED = "ack_failed"


# Outcomes that are permanently invalid for the current session/consumer or already handled:
# the Redis entry is terminally ACKed so a poison message can never jam the group forever.
_TERMINAL_ACK_OUTCOMES = frozenset(
    {
        MessageOutcome.APPLIED,
        MessageOutcome.DUPLICATE,
        MessageOutcome.STALE_TRADING_DATE,
        MessageOutcome.FUTURE_TRADING_DATE,
        MessageOutcome.OLDER_UNIVERSE,
        MessageOutcome.NEWER_UNIVERSE,
        MessageOutcome.UNKNOWN_UNIVERSE,
        MessageOutcome.ENVELOPE_DECODE_FAILED,
        MessageOutcome.UNSUPPORTED_SCHEMA,
        MessageOutcome.PAYLOAD_DECODE_FAILED,
        MessageOutcome.EVENT_KIND_MISMATCH,
    }
)


@runtime_checkable
class ShadowMarketEventSink(Protocol):
    """Non-authoritative destination for validated shadow events (Phase H connects a comparator).

    Implementations MUST NOT drive the production TickEngine/EventBus, StrategyManager, scanner,
    sector runtime, orders, or DB — they may only record/compare canonical events.
    """

    async def apply(self, envelope: MarketEventEnvelope, event: IpcPayload) -> None:
        """Apply one validated, deduplicated shadow event (record/compare only)."""
        ...


class RecordingShadowSink:
    """Reference sink: keeps a bounded ring of recently applied events for tests/inspection."""

    def __init__(self, max_entries: int = 1_000) -> None:
        self._events: deque[tuple[MarketEventEnvelope, IpcPayload]] = deque(maxlen=max_entries)
        self._applied_total = 0

    async def apply(self, envelope: MarketEventEnvelope, event: IpcPayload) -> None:
        """Record the applied event (non-authoritative)."""
        self._events.append((envelope, event))
        self._applied_total += 1

    @property
    def applied_total(self) -> int:
        """Total events applied to this sink."""
        return self._applied_total

    @property
    def events(self) -> list[tuple[MarketEventEnvelope, IpcPayload]]:
        """Snapshot of the retained (envelope, event) pairs."""
        return list(self._events)


class ConsumerDiagnostics(BaseModel):
    """Bounded, credential-free consumer health snapshot (no per-instrument cardinality)."""

    model_config = ConfigDict(frozen=True)

    running: bool
    consumer_name: str
    consumer_group: str
    stream_name: str
    received_total: int
    claimed_total: int
    applied_total: int
    acked_total: int
    duplicate_total: int
    envelope_decode_failures: int
    payload_decode_failures: int
    unsupported_schema_total: int
    event_kind_mismatch_total: int
    stale_trading_date_total: int
    future_trading_date_total: int
    older_universe_total: int
    newer_universe_total: int
    unknown_universe_total: int
    sink_failures: int
    ack_failures: int
    read_failures: int
    last_received_at: datetime | None
    last_applied_at: datetime | None
    last_ack_at: datetime | None
    last_error_at: datetime | None
    last_event_age_ms: float | None


class MarketEventConsumer:
    """Shadow consumer: read -> validate -> dedup -> decode -> sink -> ACK; failure-isolated."""

    def __init__(
        self,
        *,
        transport: MarketEventStream,
        config: MarketIpcConfig,
        sink: ShadowMarketEventSink,
        trading_date_source: TradingDateAuthority,
        universe_version_source: UniverseVersionAuthority,
        now: Callable[[], datetime],
        deduplicator: BoundedDeduplicator | None = None,
    ) -> None:
        self._transport = transport
        self._config = config
        self._sink = sink
        self._trading_date_source = trading_date_source
        self._universe_version_source = universe_version_source
        self._now = now
        # `is not None`, not `or`: an empty BoundedDeduplicator is falsy (it defines __len__).
        if deduplicator is None:
            deduplicator = BoundedDeduplicator(config.dedup_max_entries)
        self._dedup = deduplicator
        self._counters = _ConsumerCounters()
        self._running = False
        self._last_received_at: datetime | None = None
        self._last_applied_at: datetime | None = None
        self._last_ack_at: datetime | None = None
        self._last_error_at: datetime | None = None
        self._last_event_age_ms: float | None = None

    async def start(self) -> None:
        """Ensure the Redis consumer group (and stream) exists; idempotent."""
        await self._transport.ensure_group()
        self._running = True

    async def poll_once(self) -> None:
        """Run one bounded cycle: reclaim stale pending, then read new entries.

        Bounded work per cycle (fairness): at most ``read_count`` reclaimed stale entries then
        at most ``read_count`` new entries, so neither a huge pending backlog nor a live burst
        can starve the other. Redis read/claim failure is counted and the cycle ends (degraded);
        it never raises so the run loop can back off.
        """
        try:
            claimed = await self._claim_stale_bounded()
        except RedisError:
            self._record_read_failure()
            return
        for entry in claimed:
            await self._handle(entry, claimed=True)
        try:
            new_entries = await self._transport.read_raw()
        except RedisError:
            self._record_read_failure()
            return
        for entry in new_entries:
            await self._handle(entry, claimed=False)

    async def _claim_stale_bounded(self) -> list[RawDeliveredEvent]:
        """Reclaim one bounded XAUTOCLAIM page (<= ``read_count``) of stale pending entries.

        One page per cycle caps single-cycle work; a pending backlog larger than ``read_count``
        is recovered across successive cycles (the leftover stays pending for the next claim),
        so neither a huge backlog nor a live burst starves the other.
        """
        _cursor, page = await self._transport.claim_page_raw(_CURSOR_START)
        return page

    async def _handle(self, entry: RawDeliveredEvent, *, claimed: bool) -> MessageOutcome:
        """Validate/dedup/decode/apply one entry and ACK per the frozen terminal policy."""
        message_id, raw = entry
        if claimed:
            self._counters.claimed += 1
        else:
            self._counters.received += 1
        self._last_received_at = self._now()

        envelope, decode_outcome = self._decode_envelope(raw)
        if envelope is None:
            return await self._finalize(message_id, decode_outcome)
        self._last_event_age_ms = self._event_age_ms(envelope)

        gate_outcome = self._gate(envelope)
        if gate_outcome is not None:
            return await self._finalize(message_id, gate_outcome)

        identity = ProducerEventIdentity.from_envelope(envelope)
        if self._dedup.contains(identity):
            self._counters.duplicate += 1
            return await self._finalize(message_id, MessageOutcome.DUPLICATE)

        event, payload_outcome = self._decode_payload(envelope)
        if event is None:
            return await self._finalize(message_id, payload_outcome)

        return await self._apply_and_ack(message_id, envelope, event, identity)

    def _decode_envelope(
        self, raw: bytes | None
    ) -> tuple[MarketEventEnvelope | None, MessageOutcome]:
        """Deserialize the envelope, distinguishing unsupported schema from malformed bytes."""
        if raw is None:
            self._counters.envelope_decode_failures += 1
            return None, MessageOutcome.ENVELOPE_DECODE_FAILED
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._counters.envelope_decode_failures += 1
            return None, MessageOutcome.ENVELOPE_DECODE_FAILED
        if isinstance(parsed, dict):
            version = parsed.get("schema_version")
            if version is not None and version not in SUPPORTED_SCHEMA_VERSIONS:
                self._counters.unsupported_schema += 1
                return None, MessageOutcome.UNSUPPORTED_SCHEMA
        try:
            return decode_envelope(raw), MessageOutcome.APPLIED
        except ValidationError:
            self._counters.envelope_decode_failures += 1
            return None, MessageOutcome.ENVELOPE_DECODE_FAILED

    def _gate(self, envelope: MarketEventEnvelope) -> MessageOutcome | None:
        """Apply the trading-date then universe-version gates; return an outcome to reject."""
        current_date = self._trading_date_source()
        if current_date is not None:
            if envelope.trading_date < current_date:
                self._counters.stale_trading_date += 1
                return MessageOutcome.STALE_TRADING_DATE
            if envelope.trading_date > current_date:
                self._counters.future_trading_date += 1
                return MessageOutcome.FUTURE_TRADING_DATE

        comparison = compare_universe_version(
            envelope.universe_version, self._universe_version_source()
        )
        if comparison is UniverseVersionComparison.MATCH:
            return None
        return self._count_universe(comparison)

    def _count_universe(self, comparison: UniverseVersionComparison) -> MessageOutcome:
        """Increment the matching universe counter and return its rejection outcome."""
        if comparison is UniverseVersionComparison.OLDER:
            self._counters.older_universe += 1
        elif comparison is UniverseVersionComparison.NEWER:
            self._counters.newer_universe += 1
        else:
            self._counters.unknown_universe += 1
        return _UNIVERSE_REJECTIONS[comparison]

    def _decode_payload(
        self, envelope: MarketEventEnvelope
    ) -> tuple[IpcPayload | None, MessageOutcome]:
        """Decode the canonical payload, classifying kind/payload mismatch vs malformed."""
        try:
            return decode_payload(envelope.event_kind, envelope.payload), MessageOutcome.APPLIED
        except (ValidationError, ValueError):
            if self._decodes_under_other_kind(envelope):
                self._counters.event_kind_mismatch += 1
                return None, MessageOutcome.EVENT_KIND_MISMATCH
            self._counters.payload_decode_failures += 1
            return None, MessageOutcome.PAYLOAD_DECODE_FAILED

    @staticmethod
    def _decodes_under_other_kind(envelope: MarketEventEnvelope) -> bool:
        """Whether the payload validates as some kind other than the one the envelope claims."""
        for other in EventKind:
            if other is envelope.event_kind:
                continue
            try:
                decode_payload(other, envelope.payload)
                return True
            except (ValidationError, ValueError):
                continue
        return False

    async def _apply_and_ack(
        self,
        message_id: str,
        envelope: MarketEventEnvelope,
        event: IpcPayload,
        identity: ProducerEventIdentity,
    ) -> MessageOutcome:
        """Apply to the shadow sink, commit dedup, then ACK; isolate sink and ACK failures."""
        try:
            await self._sink.apply(envelope, event)
        except Exception:  # noqa: BLE001 - a sink fault must not kill the consumer; leave pending
            self._counters.sink_failures += 1
            self._last_error_at = self._now()
            return MessageOutcome.SINK_FAILED  # not ACKed -> redelivered and retried
        self._dedup.record(identity)  # committed only after successful application
        self._counters.applied += 1
        self._last_applied_at = self._now()
        return await self._finalize(message_id, MessageOutcome.APPLIED)

    async def _finalize(self, message_id: str, outcome: MessageOutcome) -> MessageOutcome:
        """ACK terminal outcomes; leave transient ones pending for redelivery."""
        if outcome not in _TERMINAL_ACK_OUTCOMES:
            return outcome
        try:
            await self._transport.ack(message_id)
        except RedisError:
            self._counters.ack_failures += 1
            self._last_error_at = self._now()
            return MessageOutcome.ACK_FAILED
        self._counters.acked += 1
        self._last_ack_at = self._now()
        return outcome

    def _event_age_ms(self, envelope: MarketEventEnvelope) -> float:
        """Milliseconds between now and the envelope's produced-at (freshness signal)."""
        return (self._now() - envelope.produced_at).total_seconds() * 1_000.0

    def _record_read_failure(self) -> None:
        """Count a Redis read/claim failure and enter the degraded (error) state."""
        self._counters.read_failures += 1
        self._last_error_at = self._now()

    def diagnostics(self) -> ConsumerDiagnostics:
        """Snapshot the bounded consumer counters and freshness markers."""
        counters = self._counters
        return ConsumerDiagnostics(
            running=self._running,
            consumer_name=self._config.consumer_name,
            consumer_group=self._config.consumer_group,
            stream_name=self._config.stream_name,
            received_total=counters.received,
            claimed_total=counters.claimed,
            applied_total=counters.applied,
            acked_total=counters.acked,
            duplicate_total=counters.duplicate,
            envelope_decode_failures=counters.envelope_decode_failures,
            payload_decode_failures=counters.payload_decode_failures,
            unsupported_schema_total=counters.unsupported_schema,
            event_kind_mismatch_total=counters.event_kind_mismatch,
            stale_trading_date_total=counters.stale_trading_date,
            future_trading_date_total=counters.future_trading_date,
            older_universe_total=counters.older_universe,
            newer_universe_total=counters.newer_universe,
            unknown_universe_total=counters.unknown_universe,
            sink_failures=counters.sink_failures,
            ack_failures=counters.ack_failures,
            read_failures=counters.read_failures,
            last_received_at=self._last_received_at,
            last_applied_at=self._last_applied_at,
            last_ack_at=self._last_ack_at,
            last_error_at=self._last_error_at,
            last_event_age_ms=self._last_event_age_ms,
        )


_UNIVERSE_REJECTIONS: dict[UniverseVersionComparison, MessageOutcome] = {
    UniverseVersionComparison.OLDER: MessageOutcome.OLDER_UNIVERSE,
    UniverseVersionComparison.NEWER: MessageOutcome.NEWER_UNIVERSE,
    UniverseVersionComparison.UNKNOWN: MessageOutcome.UNKNOWN_UNIVERSE,
}


class _ConsumerCounters:
    """Mutable bounded counters (fixed field set; no unbounded per-instrument growth)."""

    __slots__ = (
        "received",
        "claimed",
        "applied",
        "acked",
        "duplicate",
        "envelope_decode_failures",
        "payload_decode_failures",
        "unsupported_schema",
        "event_kind_mismatch",
        "stale_trading_date",
        "future_trading_date",
        "older_universe",
        "newer_universe",
        "unknown_universe",
        "sink_failures",
        "ack_failures",
        "read_failures",
    )

    def __init__(self) -> None:
        self.received = 0
        self.claimed = 0
        self.applied = 0
        self.acked = 0
        self.duplicate = 0
        self.envelope_decode_failures = 0
        self.payload_decode_failures = 0
        self.unsupported_schema = 0
        self.event_kind_mismatch = 0
        self.stale_trading_date = 0
        self.future_trading_date = 0
        self.older_universe = 0
        self.newer_universe = 0
        self.unknown_universe = 0
        self.sink_failures = 0
        self.ack_failures = 0
        self.read_failures = 0
