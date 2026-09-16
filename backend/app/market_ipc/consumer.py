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
Dedup is pluggable (:class:`Deduplicator`): the default is a NON-durable in-memory window
(Phase-C behaviour), while C1 wiring injects a durable Redis authority (:class:`Composite\
Deduplicator`) so a completed application is recognised across process/consumer restart and
Redis Stream redelivery. A durable dedup store failure is fail-closed (entry left pending, never
applied blind). Exactly-once is still NOT claimed: the residual "applied but the durable mark
had not yet committed" window can re-apply an event on redelivery — harmless for the current
non-authoritative shadow sink; total Redis data loss discards stream and dedup state together.
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

from app.market_ipc.config import MarketIpcConfig, validate_retention_invariant
from app.market_ipc.dedup import BoundedDeduplicator
from app.market_ipc.durable_dedup import Deduplicator, InMemoryDeduplicator
from app.market_ipc.envelope import (
    SUPPORTED_SCHEMA_VERSIONS,
    MarketEventEnvelope,
    ProducerEventIdentity,
    UniverseVersionComparison,
    compare_universe_version,
    decode_envelope,
)
from app.market_ipc.events import EventKind, IpcPayload, decode_payload
from app.market_ipc.loss_detection import ConsumerProgressEvidence
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
    DEDUP_UNAVAILABLE = "dedup_unavailable"  # durable dedup store failed — fail closed, retry
    BEYOND_HORIZON = "beyond_horizon"  # older than the redelivery horizon — dropped, never applied


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
        MessageOutcome.BEYOND_HORIZON,
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
    dedup_store_failures: int
    beyond_horizon_total: int
    # H4B pending-recovery (XAUTOCLAIM) counters — bounded scalars, no per-entry cardinality.
    pending_recovery_runs: int
    pending_reclaimed: int
    pending_reclaim_failures: int
    pending_reclaimed_applied: int
    pending_reclaimed_duplicates: int
    last_applied_epoch: int | None
    last_applied_sequence: int | None
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
        deduplicator: Deduplicator | None = None,
    ) -> None:
        self._transport = transport
        self._config = config
        self._sink = sink
        self._trading_date_source = trading_date_source
        self._universe_version_source = universe_version_source
        self._now = now
        # Default is the NON-durable in-memory window (Phase-C behaviour); production C1 wiring
        # injects a CompositeDeduplicator (durable Redis authority + in-memory cache).
        if deduplicator is None:
            deduplicator = InMemoryDeduplicator(BoundedDeduplicator(config.dedup_max_entries))
        self._dedup = deduplicator
        self._counters = _ConsumerCounters()
        self._running = False
        self._last_applied_epoch: int | None = None
        self._last_applied_sequence: int | None = None
        self._last_received_at: datetime | None = None
        self._last_applied_at: datetime | None = None
        self._last_ack_at: datetime | None = None
        self._last_error_at: datetime | None = None
        self._last_event_age_ms: float | None = None

    @property
    def sink(self) -> ShadowMarketEventSink:
        """The destination events are applied to (so a runtime can seed it before the poll loop)."""
        return self._sink

    async def start(self) -> None:
        """Ensure the Redis consumer group (and stream) exists; idempotent. Fail closed on B4.

        The retention invariant is re-checked here (not only at config construction) so a config
        mutated via ``model_copy`` — which bypasses validation — can never start an unsafe consumer.
        """
        validate_retention_invariant(self._config)
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
            self._counters.reclaim_failures += 1  # recovery-scoped view of the claim fault
            self._record_read_failure()
            return
        self._counters.recovery_runs += 1
        for entry in claimed:
            self._record_reclaimed(await self._handle(entry, claimed=True))
        try:
            new_entries = await self._transport.read_raw()
        except RedisError:
            self._record_read_failure()
            return
        for entry in new_entries:
            await self._handle(entry, claimed=False)

    def _record_reclaimed(self, outcome: MessageOutcome) -> None:
        """Attribute one reclaimed entry's terminal outcome to the recovery-scoped counters.

        A reclaimed entry that failed transiently (sink/mark/ack) counts in neither applied nor
        duplicate — it stays pending and is retried on a later recovery pass.
        """
        if outcome is MessageOutcome.APPLIED:
            self._counters.reclaimed_applied += 1
        elif outcome is MessageOutcome.DUPLICATE:
            self._counters.reclaimed_duplicates += 1

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
        age_ms = self._event_age_ms(envelope)
        self._last_event_age_ms = age_ms

        # B4 (ADR-028): an event older than the redelivery horizon is dropped, never applied. This
        # is the publish-independent horizon bound — it holds even when the market is quiet and no
        # producer trim runs, so a reclaimed pending entry whose dedup key may have expired can
        # never be re-applied (it is lost — safe — not double-applied). Within the horizon the
        # config invariant guarantees the dedup key still exists to suppress a genuine redelivery.
        if age_ms > self._config.max_redelivery_horizon_seconds * 1_000:
            self._counters.beyond_horizon += 1
            return await self._finalize(message_id, MessageOutcome.BEYOND_HORIZON)

        gate_outcome = self._gate(envelope)
        if gate_outcome is not None:
            return await self._finalize(message_id, gate_outcome)

        identity = ProducerEventIdentity.from_envelope(envelope)
        try:
            already_applied = await self._dedup.contains(identity)
        except RedisError:
            return self._record_dedup_failure()  # fail closed: leave pending, never apply blind
        if already_applied:
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
        try:
            await self._dedup.record(identity)  # durable mark, only after successful application
        except RedisError:
            # Applied, but the durable mark did not commit: fail closed (do NOT ACK). The entry
            # redelivers; the shadow sink re-applies harmlessly (no durable/business effect). An
            # authoritative sink would require an atomic sink+mark transition (ADR-023).
            return self._record_dedup_failure()
        self._counters.applied += 1
        self._last_applied_at = self._now()
        self._advance_last_applied(identity)
        return await self._finalize(message_id, MessageOutcome.APPLIED)

    def _advance_last_applied(self, identity: ProducerEventIdentity) -> None:
        """Track the highest durably-applied ``(epoch, sequence)`` (B11 consumer progress; H9B).

        Monotonic within a lineage: a newer epoch replaces the position; a higher sequence in the
        current epoch advances it; a reclaimed/out-of-order older entry never rewinds it. Called
        only after the durable dedup mark commits, so it reflects successfully applied progress —
        never a merely-read event (preserves the H8A apply→mark boundary).
        """
        epoch, sequence = identity.producer_epoch, identity.producer_sequence
        if self._last_applied_epoch is None or epoch > self._last_applied_epoch:
            self._last_applied_epoch = epoch
            self._last_applied_sequence = sequence
        elif epoch == self._last_applied_epoch and (
            self._last_applied_sequence is None or sequence > self._last_applied_sequence
        ):
            self._last_applied_sequence = sequence

    def consumer_progress(self) -> ConsumerProgressEvidence:
        """The last canonical identity this consumer durably applied (B11 evidence input)."""
        return ConsumerProgressEvidence(
            last_applied_epoch=self._last_applied_epoch,
            last_applied_sequence=self._last_applied_sequence,
        )

    def _record_dedup_failure(self) -> MessageOutcome:
        """Count a durable-dedup store failure and fail closed (entry left pending)."""
        self._counters.dedup_store_failures += 1
        self._last_error_at = self._now()
        return MessageOutcome.DEDUP_UNAVAILABLE

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
            dedup_store_failures=counters.dedup_store_failures,
            beyond_horizon_total=counters.beyond_horizon,
            pending_recovery_runs=counters.recovery_runs,
            pending_reclaimed=counters.claimed,
            pending_reclaim_failures=counters.reclaim_failures,
            pending_reclaimed_applied=counters.reclaimed_applied,
            pending_reclaimed_duplicates=counters.reclaimed_duplicates,
            last_applied_epoch=self._last_applied_epoch,
            last_applied_sequence=self._last_applied_sequence,
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
        "dedup_store_failures",
        "beyond_horizon",
        "recovery_runs",
        "reclaim_failures",
        "reclaimed_applied",
        "reclaimed_duplicates",
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
        self.dedup_store_failures = 0
        self.beyond_horizon = 0
        self.recovery_runs = 0
        self.reclaim_failures = 0
        self.reclaimed_applied = 0
        self.reclaimed_duplicates = 0
