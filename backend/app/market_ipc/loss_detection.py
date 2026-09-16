"""Consume-side Redis loss / continuity reconciliation for market IPC (DECOUPLING PHASE H8C, B11).

Producer-side L1 confirms delivery at D1's ``XADD`` ack and therefore **cannot** observe a later
Redis loss (an AOF ``everysec`` tail-loss, a reset/``FLUSHALL``, or a restore of an older snapshot).
This module is the frozen ADR-025 answer: a **consume-side loss detector reconciled against the
producer's L1 record**. It classifies the transport's continuity by reconciling three evidence
sources at a point in time and never persists any state of its own:

    PRODUCER (L1, survives Redis loss — the epoch file lives on the ingestion host, not Redis):
        producer_id, producer_epoch, last **published** (D1-confirmed) sequence, terminal break
    REDIS (bounded, Redis-native metadata — no unbounded stream scan):
        XINFO STREAM (length, last-generated-id, first-entry), XINFO GROUPS (last-delivered-id,
        pending), and one XREVRANGE(COUNT 1) to read the last entry's canonical identity
    CONSUMER:
        the last canonical identity the backend durably applied

Hard rules (ADR-025 / DESIGN-REVIEW-2):

* **Never infer loss from producer-sequence arithmetic.** A gap between allocated sequences is
  legal (M2 allocates before admission; overflow/reject leaves a hole). Reconciliation compares the
  producer's last **published** position (L1, D1-confirmed) against what Redis/the consumer account
  for — never "sequence N+1 is missing".
* **Incarnation-scoped.** All reasoning is within one ``(producer_id, producer_epoch)``; a new epoch
  is a new incarnation, never a rewind.
* **Fail closed.** Missing Redis metadata, an uncertain producer publication outcome, or any
  unresolved reset/rewind/unaccounted-publication yields ``ready_for_authority=False``. This is a
  readiness *input*; it activates nothing (Phase H9 owns authority).

Off by default — nothing composes it into the production runtime. It never touches the TickEngine/
MarketContext, a Dhan broker, or a strategy; it only reads bounded Redis metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.market_ipc.config import MarketIpcConfig
from app.market_ipc.envelope import ProducerEventIdentity, decode_envelope
from app.market_ipc.transport import _FIELD

if TYPE_CHECKING:
    from app.market_ipc.continuity import FeedContinuitySnapshot
    from app.market_ipc.state import IngestionHealthState

_ORIGIN_ID = "0-0"  # a stream that has never had an entry generated


class LossDetectionState(StrEnum):
    """Reconciled transport continuity classification (never a bare healthy/unhealthy boolean)."""

    HEALTHY = "healthy"
    CONSUMER_LAGGING = "consumer_lagging"  # events retained; the consumer is simply behind
    PENDING_RECOVERY = "pending_recovery"  # published + delivered, awaiting XAUTOCLAIM/ACK
    RETENTION_EXPECTED = "retention_expected"  # applied, then legitimately aged out (H8B)
    PRODUCER_PUBLICATION_FAILED = "producer_publication_failed"  # L1 broke — not a Redis loss
    REDIS_STREAM_RESET = "redis_stream_reset"  # stream/group reinitialised under a live producer
    REDIS_STATE_REWIND = "redis_state_rewind"  # group delivered past the stream's last id
    PUBLISHED_EVENT_UNACCOUNTED_FOR = (
        "published_event_unaccounted_for"  # tail-loss of confirmed pubs
    )
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"  # metadata unavailable / outcome uncertain


# States compatible with (eventual) authority: the transport is trustworthy or merely catching up.
_READY_STATES = frozenset(
    {
        LossDetectionState.HEALTHY,
        LossDetectionState.CONSUMER_LAGGING,
        LossDetectionState.PENDING_RECOVERY,
        LossDetectionState.RETENTION_EXPECTED,
    }
)


@dataclass(frozen=True, slots=True)
class ProducerPublicationEvidence:
    """The producer L1 facts the reconciler needs (survives a Redis loss)."""

    producer_id: str
    producer_epoch: int
    last_published_sequence: int | None
    terminal_publication_break: bool
    publication_outcome_uncertain: bool

    @classmethod
    def from_continuity(cls, snapshot: FeedContinuitySnapshot) -> ProducerPublicationEvidence:
        """Project an L1 :class:`FeedContinuitySnapshot` into producer evidence.

        Uses the **published** (D1-confirmed) position, never the accepted/allocated one, and keeps
        the terminal-break and uncertain-outcome distinctions so downstream absence is attributed to
        the producer rather than to Redis.
        """
        from app.market_ipc.continuity import ContinuityReason, ContinuityState

        assert snapshot.producer_id is not None and snapshot.producer_epoch is not None
        return cls(
            producer_id=snapshot.producer_id,
            producer_epoch=snapshot.producer_epoch,
            last_published_sequence=snapshot.last_published_sequence,
            terminal_publication_break=snapshot.state is ContinuityState.BROKEN,
            publication_outcome_uncertain=(
                snapshot.reason is ContinuityReason.PUBLICATION_OUTCOME_UNCERTAIN
            ),
        )

    @classmethod
    def from_ingestion_health(cls, state: IngestionHealthState) -> ProducerPublicationEvidence:
        """Project a decoded ``md:health`` snapshot into producer evidence (H9B conveyance).

        The md:health L1 fields were written by the producer directly from the same
        :class:`FeedContinuitySnapshot` that :meth:`from_continuity` reads, so this reconstitutes
        identical evidence across the process boundary — one health truth, no re-derivation.
        """
        return cls(
            producer_id=state.producer_id,
            producer_epoch=state.producer_epoch,
            last_published_sequence=state.last_published_sequence,
            terminal_publication_break=state.terminal_publication_break,
            publication_outcome_uncertain=state.publication_outcome_uncertain,
        )


@dataclass(frozen=True, slots=True)
class ConsumerProgressEvidence:
    """The last canonical identity the backend durably applied (None if it has applied nothing)."""

    last_applied_epoch: int | None = None
    last_applied_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class StreamMetadata:
    """Bounded Redis-native metadata for one stream + consumer group (no unbounded scan)."""

    stream_exists: bool
    length: int
    last_generated_id: str
    group_exists: bool
    group_last_delivered_id: str
    pending: int
    last_entry_identity: ProducerEventIdentity | None


@dataclass(frozen=True, slots=True)
class LossDetectionResult:
    """Bounded, credential-free reconciliation result (diagnostics + the readiness input)."""

    state: LossDetectionState
    reason: str
    ready_for_authority: bool
    producer_id: str
    producer_epoch: int
    producer_last_published_sequence: int | None
    consumer_last_applied_sequence: int | None
    stream_length: int
    stream_last_generated_id: str
    group_last_delivered_id: str
    pending: int


def _parse_id(stream_id: str) -> tuple[int, int]:
    """Parse a ``<ms>-<seq>`` Redis stream id into a comparable tuple; ``0-0`` for anything odd."""
    try:
        ms, seq = stream_id.split("-")
        return int(ms), int(seq)
    except (ValueError, AttributeError):
        return 0, 0


def _id_gt(left: str, right: str) -> bool:
    """Whether stream id ``left`` sorts strictly after ``right`` (id ordering, not arithmetic)."""
    return _parse_id(left) > _parse_id(right)


def _result(
    state: LossDetectionState,
    reason: str,
    producer: ProducerPublicationEvidence,
    consumer: ConsumerProgressEvidence,
    metadata: StreamMetadata,
) -> LossDetectionResult:
    return LossDetectionResult(
        state=state,
        reason=reason,
        ready_for_authority=state in _READY_STATES,
        producer_id=producer.producer_id,
        producer_epoch=producer.producer_epoch,
        producer_last_published_sequence=producer.last_published_sequence,
        consumer_last_applied_sequence=consumer.last_applied_sequence,
        stream_length=metadata.length,
        stream_last_generated_id=metadata.last_generated_id,
        group_last_delivered_id=metadata.group_last_delivered_id,
        pending=metadata.pending,
    )


def reconcile(
    producer: ProducerPublicationEvidence,
    consumer: ConsumerProgressEvidence,
    metadata: StreamMetadata,
) -> LossDetectionResult:
    """Classify transport continuity from the three evidence snapshots (pure, no I/O).

    Ordering matters: producer-cause first (a broken/uncertain producer is never a Redis loss),
    then Redis reset/rewind, then producer↔stream tail reconciliation, then consumer lag/pending.
    """
    if producer.publication_outcome_uncertain:
        return _result(
            LossDetectionState.INSUFFICIENT_EVIDENCE,
            "producer publication outcome uncertain",
            producer,
            consumer,
            metadata,
        )
    if producer.terminal_publication_break:
        return _result(
            LossDetectionState.PRODUCER_PUBLICATION_FAILED,
            "producer L1 reports a terminal publication break",
            producer,
            consumer,
            metadata,
        )
    if producer.last_published_sequence is None:
        return _result(
            LossDetectionState.HEALTHY,
            "producer has published nothing yet",
            producer,
            consumer,
            metadata,
        )

    # The producer confirmed publications; Redis must be able to account for them.
    if not metadata.stream_exists or metadata.last_generated_id == _ORIGIN_ID:
        return _result(
            LossDetectionState.REDIS_STREAM_RESET,
            "producer published but the stream is absent or was never written",
            producer,
            consumer,
            metadata,
        )
    if not metadata.group_exists:
        return _result(
            LossDetectionState.REDIS_STREAM_RESET,
            "the consumer group is absent under a producer that has published",
            producer,
            consumer,
            metadata,
        )
    # Rewind: the group durably delivered an id newer than any the stream now holds.
    if _id_gt(metadata.group_last_delivered_id, metadata.last_generated_id):
        return _result(
            LossDetectionState.REDIS_STATE_REWIND,
            "group last-delivered-id is ahead of the stream's last-generated-id",
            producer,
            consumer,
            metadata,
        )
    return _reconcile_present(producer, consumer, metadata)


def _reconcile_present(
    producer: ProducerPublicationEvidence,
    consumer: ConsumerProgressEvidence,
    metadata: StreamMetadata,
) -> LossDetectionResult:
    """Reconcile when the stream and group exist and have not rewound."""
    published = producer.last_published_sequence
    assert published is not None
    last = metadata.last_entry_identity
    if last is None:
        # The stream generated ids but retains no entry: everything was trimmed. Safe only if the
        # consumer already applied the producer's published position (H8B: applied-then-aged-out).
        if _consumer_caught_up(consumer, producer):
            return _result(
                LossDetectionState.RETENTION_EXPECTED,
                "all entries applied then legitimately trimmed (H8B horizon)",
                producer,
                consumer,
                metadata,
            )
        return _result(
            LossDetectionState.PUBLISHED_EVENT_UNACCOUNTED_FOR,
            "stream retains no entry yet published events are not accounted for",
            producer,
            consumer,
            metadata,
        )
    if last.producer_epoch != producer.producer_epoch:
        return _result(
            LossDetectionState.INSUFFICIENT_EVIDENCE,
            "stream tail belongs to a different producer incarnation",
            producer,
            consumer,
            metadata,
        )
    if last.producer_sequence < published:
        # The producer confirmed publishing up to `published`, but the stream tail stops short — the
        # confirmed tail is gone (e.g. an AOF everysec tail-loss). Never inferred from a gap.
        return _result(
            LossDetectionState.PUBLISHED_EVENT_UNACCOUNTED_FOR,
            "stream tail is behind the producer's confirmed published position (tail loss)",
            producer,
            consumer,
            metadata,
        )
    # All confirmed publications are present in the stream.
    if _consumer_caught_up(consumer, producer):
        return _result(
            LossDetectionState.HEALTHY,
            "consumer caught up to the producer",
            producer,
            consumer,
            metadata,
        )
    if metadata.pending > 0:
        return _result(
            LossDetectionState.PENDING_RECOVERY,
            "published events delivered and awaiting recovery/ACK",
            producer,
            consumer,
            metadata,
        )
    return _result(
        LossDetectionState.CONSUMER_LAGGING,
        "published events retained in the stream; the consumer is behind",
        producer,
        consumer,
        metadata,
    )


def _consumer_caught_up(
    consumer: ConsumerProgressEvidence, producer: ProducerPublicationEvidence
) -> bool:
    """Whether the consumer has durably applied the producer's published position (same epoch)."""
    return (
        consumer.last_applied_epoch == producer.producer_epoch
        and consumer.last_applied_sequence is not None
        and producer.last_published_sequence is not None
        and consumer.last_applied_sequence >= producer.last_published_sequence
    )


class RedisLossDetector:
    """Reads bounded Redis metadata and reconciles it against producer/consumer evidence (B11)."""

    def __init__(self, redis: Redis, config: MarketIpcConfig) -> None:
        self._redis = redis
        self._config = config

    async def evaluate(
        self, producer: ProducerPublicationEvidence, consumer: ConsumerProgressEvidence
    ) -> LossDetectionResult:
        """Reconcile the transport state now; fail closed (INSUFFICIENT_EVIDENCE) on any error."""
        try:
            metadata = await self._read_metadata()
        except RedisError:
            return LossDetectionResult(
                state=LossDetectionState.INSUFFICIENT_EVIDENCE,
                reason="redis metadata unavailable",
                ready_for_authority=False,
                producer_id=producer.producer_id,
                producer_epoch=producer.producer_epoch,
                producer_last_published_sequence=producer.last_published_sequence,
                consumer_last_applied_sequence=consumer.last_applied_sequence,
                stream_length=0,
                stream_last_generated_id=_ORIGIN_ID,
                group_last_delivered_id=_ORIGIN_ID,
                pending=0,
            )
        return reconcile(producer, consumer, metadata)

    async def _read_metadata(self) -> StreamMetadata:
        """Bounded metadata: XINFO STREAM + XINFO GROUPS + one XREVRANGE (last entry). O(1)."""
        stream = self._config.stream_name
        try:
            info = await self._redis.xinfo_stream(stream)
        except RedisError as error:
            if "no such key" in str(error).lower():
                return StreamMetadata(False, 0, _ORIGIN_ID, False, _ORIGIN_ID, 0, None)
            raise
        length = int(info.get("length", 0))
        last_generated_id = _as_str(info.get("last-generated-id", _ORIGIN_ID))
        group_exists, group_last_delivered_id, pending = await self._read_group()
        last_entry_identity = await self._read_last_identity()
        return StreamMetadata(
            stream_exists=True,
            length=length,
            last_generated_id=last_generated_id,
            group_exists=group_exists,
            group_last_delivered_id=group_last_delivered_id,
            pending=pending,
            last_entry_identity=last_entry_identity,
        )

    async def _read_group(self) -> tuple[bool, str, int]:
        groups = await self._redis.xinfo_groups(self._config.stream_name)
        for group in groups:
            if _as_str(group.get("name")) == self._config.consumer_group:
                return (
                    True,
                    _as_str(group.get("last-delivered-id", _ORIGIN_ID)),
                    int(group.get("pending", 0)),
                )
        return False, _ORIGIN_ID, 0

    async def _read_last_identity(self) -> ProducerEventIdentity | None:
        entries = await self._redis.xrevrange(self._config.stream_name, count=1)
        if not entries:
            return None
        _message_id, fields = entries[0]
        if fields is None:  # a trimmed/tombstone tail entry carries no payload
            return None
        raw = fields.get(_FIELD) or fields.get(_FIELD.encode())
        if raw is None:
            return None
        return ProducerEventIdentity.from_envelope(decode_envelope(raw))


def _as_str(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
