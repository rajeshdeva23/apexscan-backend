"""Versioned broker-neutral IPC envelope + identity/version value objects (PHASE A).

The envelope wraps one canonical payload with the metadata a decoupled ingestion producer
and backend consumer need: schema version, producer identity/epoch/sequence, produced-at,
event kind, trading date, universe version, and instrument identity. Serialization is
deterministic JSON (no pickle, no arbitrary-code deserialization); Decimal precision and
timezone-awareness are preserved by the payload's own canonical JSON.

Compatibility (DESIGN-REVIEW-2 schema policy): additive unknown fields at the IPC boundary
are tolerated (``extra="ignore"``); an unsupported schema version or any malformed required
field fails closed. This relaxation lives ONLY here — canonical domain models keep their
strict validation untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.market_ipc.events import EventKind, IpcPayload, encode_payload, event_kind_for

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

FEED_WIDE_IDENTITY = "*:*"
_MAX_PAYLOAD_BYTES = 262_144  # hard safety ceiling; MarketIpcConfig may set a stricter bound


def _require_aware(value: datetime) -> datetime:
    """Reject naive timestamps and normalize to UTC (no ambiguous clocks on the wire)."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("produced_at must be timezone-aware")
    return value.astimezone(UTC)


class MarketEventEnvelope(BaseModel):
    """One versioned IPC event: metadata + a canonical payload carried as its JSON string."""

    model_config = ConfigDict(
        extra="ignore",  # tolerate additive fields from a newer producer (fail-open, boundary only)
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    schema_version: int
    producer_id: str = Field(min_length=1, max_length=128)
    producer_epoch: int = Field(ge=0)
    producer_sequence: int = Field(ge=0)
    produced_at: datetime
    event_kind: EventKind
    trading_date: date
    universe_version: int = Field(ge=0)
    instrument_identity: str = Field(min_length=1, max_length=128)
    payload: str = Field(min_length=1)

    _validate_produced_at = field_validator("produced_at")(_require_aware)

    @field_validator("schema_version")
    @classmethod
    def _supported_schema_version(cls, value: int) -> int:
        if value not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"unsupported schema_version {value}; supported={sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )
        return value

    @field_validator("instrument_identity")
    @classmethod
    def _well_formed_identity(cls, value: str) -> str:
        exchange, sep, symbol = value.partition(":")
        if not sep or not exchange or not symbol:
            raise ValueError("instrument_identity must be 'EXCHANGE:SYMBOL'")
        return value

    @field_validator("payload")
    @classmethod
    def _bounded_payload(cls, value: str) -> str:
        if len(value.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise ValueError("payload exceeds maximum permitted size")
        return value


def identity_string_for(payload: IpcPayload) -> str:
    """Derive the envelope instrument identity for a canonical payload.

    Feed-wide events (no instrument) map to the reserved feed-wide identity; per-instrument
    events map to ``EXCHANGE:SYMBOL``. Full derivative identity lives in the payload.
    """
    instrument = getattr(payload, "instrument", None)
    if instrument is None:
        return FEED_WIDE_IDENTITY
    return f"{instrument.exchange}:{instrument.symbol}"


def build_envelope(
    payload: IpcPayload,
    *,
    producer_id: str,
    producer_epoch: int,
    producer_sequence: int,
    produced_at: datetime,
    trading_date: date,
    universe_version: int,
) -> MarketEventEnvelope:
    """Wrap a canonical payload in a schema-current envelope (used by Phase B and tests)."""
    return MarketEventEnvelope(
        schema_version=SCHEMA_VERSION,
        producer_id=producer_id,
        producer_epoch=producer_epoch,
        producer_sequence=producer_sequence,
        produced_at=produced_at,
        event_kind=event_kind_for(payload),
        trading_date=trading_date,
        universe_version=universe_version,
        instrument_identity=identity_string_for(payload),
        payload=encode_payload(payload),
    )


def encode_envelope(envelope: MarketEventEnvelope, *, max_bytes: int = _MAX_PAYLOAD_BYTES) -> bytes:
    """Serialize an envelope to deterministic UTF-8 JSON bytes, bounding total size."""
    raw = envelope.model_dump_json().encode("utf-8")
    if len(raw) > max_bytes:
        raise ValueError(f"encoded envelope {len(raw)}B exceeds max_bytes {max_bytes}")
    return raw


def decode_envelope(raw: bytes | str) -> MarketEventEnvelope:
    """Deserialize and validate an envelope, failing closed on any malformed field."""
    return MarketEventEnvelope.model_validate_json(raw)


@dataclass(frozen=True, slots=True)
class ProducerEventIdentity:
    """The dedup identity for one produced event.

    Includes ``producer_epoch`` because a restarted producer resets ``producer_sequence`` to
    zero (DESIGN-REVIEW-2 correction); ``(producer_id, producer_sequence)`` alone would treat
    post-restart events as duplicates. The epoch disambiguates restarts.
    """

    producer_id: str
    producer_epoch: int
    producer_sequence: int

    @classmethod
    def from_envelope(cls, envelope: MarketEventEnvelope) -> ProducerEventIdentity:
        """Extract the dedup identity from an envelope."""
        return cls(envelope.producer_id, envelope.producer_epoch, envelope.producer_sequence)


class UniverseVersionComparison(StrEnum):
    """Result of comparing an event's universe version to the consumer's authority."""

    MATCH = "match"
    OLDER = "older"
    NEWER = "newer"
    UNKNOWN = "unknown"


def compare_universe_version(
    event_version: int, current_version: int | None
) -> UniverseVersionComparison:
    """Classify an event's universe version against the consumer's current version.

    ``UNKNOWN`` when the consumer has no authoritative version yet. Phase A defines the
    contract only; automatic reconciliation is a later phase.
    """
    if current_version is None:
        return UniverseVersionComparison.UNKNOWN
    if event_version == current_version:
        return UniverseVersionComparison.MATCH
    return (
        UniverseVersionComparison.OLDER
        if event_version < current_version
        else UniverseVersionComparison.NEWER
    )


def is_stale_trading_date(event_date: date, current_trading_date: date | None) -> bool:
    """Whether an event predates the authoritative trading date (a future consumer rejects it).

    Not wired into the production TickEngine in Phase A — this is the reusable predicate only.
    """
    return current_trading_date is not None and event_date < current_trading_date
