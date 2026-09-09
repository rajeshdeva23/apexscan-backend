"""Compacted reference-state + ingestion-health contracts (PHASE A).

Two pieces of Redis-backed *state* (distinct from the ordered event stream), defined here as
schema + key-naming + a store interface with an in-memory reference implementation:

* Compacted reference state (``md:reference:<trading_date>``): the last-known canonical
  reference per instrument, so a restarted backend can recover ``previous_close`` /
  session-open deterministically without re-catching the one-time at-subscribe frames
  (DESIGN-REVIEW-2 §12). It NEVER fabricates values — a field is present only after the
  canonical value was received. Dhan code-6 remains the previous_close authority; no
  historical fallback is introduced.
* Ingestion health (``md:health``): a broker-neutral snapshot of the future ingestion
  process's health signals. Defining the shape here does not change ``/health/ready``.

Phase A wires none of this into production; there is no writer to Redis yet.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.market_ipc.config import MarketIpcConfig
from app.schemas.market_data import ProviderStatus


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class CompactedReferenceState(BaseModel):
    """Last-known canonical reference for one instrument on one trading date.

    Every price field is optional and is set only once its canonical value has arrived; this
    model never derives, infers, or substitutes a value (notably ``session_open``).

    Carries producer provenance ``(producer_id, producer_epoch, producer_sequence)`` and
    ``universe_version`` (Phase D): compaction is monotonic by ``(producer_epoch,
    producer_sequence)`` so a stale/replayed update never overwrites newer state, and recovery
    can gate on universe version. Provenance records where in producer ordering the state came
    from — it is never a fabricated price.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    instrument_identity: str = Field(min_length=1, max_length=128)
    trading_date: date
    updated_at: datetime
    universe_version: int = Field(ge=0)
    producer_id: str = Field(min_length=1, max_length=128)
    producer_epoch: int = Field(ge=0)
    producer_sequence: int = Field(ge=0)
    previous_close: Decimal | None = Field(default=None, gt=0)
    session_open: Decimal | None = Field(default=None, gt=0)
    session_high: Decimal | None = Field(default=None, gt=0)
    session_low: Decimal | None = Field(default=None, gt=0)
    session_close: Decimal | None = Field(default=None, gt=0)

    _validate_updated_at = field_validator("updated_at")(_require_aware)

    @property
    def ordering(self) -> tuple[int, int]:
        """Monotonic compaction key within a producer lineage (epoch, then sequence)."""
        return (self.producer_epoch, self.producer_sequence)


def reference_key(prefix: str, trading_date: date) -> str:
    """Redis key for the compacted reference hash of one trading date."""
    return f"{prefix}:{trading_date.isoformat()}"


@runtime_checkable
class CompactedReferenceStore(Protocol):
    """The interface the future backend uses to recover reference state on restart.

    Keyed by trading date so recovery is isolated to the current session; a store for one
    date never returns another date's values.
    """

    async def put(self, state: CompactedReferenceState) -> None:
        """Upsert the compacted reference for one instrument on its trading date."""
        ...

    async def get(
        self, trading_date: date, instrument_identity: str
    ) -> CompactedReferenceState | None:
        """Return one instrument's reference for a trading date, or ``None``."""
        ...

    async def all(self, trading_date: date) -> tuple[CompactedReferenceState, ...]:
        """Return every instrument's reference for one trading date."""
        ...


class InMemoryCompactedReferenceStore:
    """Reference/test implementation of :class:`CompactedReferenceStore`.

    Enforces trading-date isolation. The Redis-backed store (hash per ``md:reference:<date>``)
    is Phase D — this proves the contract and round-trip without touching Redis.
    """

    def __init__(self) -> None:
        self._by_date: dict[date, dict[str, CompactedReferenceState]] = {}

    async def put(self, state: CompactedReferenceState) -> None:
        """Store the reference under its trading date and instrument identity."""
        self._by_date.setdefault(state.trading_date, {})[state.instrument_identity] = state

    async def get(
        self, trading_date: date, instrument_identity: str
    ) -> CompactedReferenceState | None:
        """Return the stored reference for the date/instrument, or ``None``."""
        return self._by_date.get(trading_date, {}).get(instrument_identity)

    async def all(self, trading_date: date) -> tuple[CompactedReferenceState, ...]:
        """Return all references stored for one trading date."""
        return tuple(self._by_date.get(trading_date, {}).values())


class IngestionHealthState(BaseModel):
    """Broker-neutral ingestion health snapshot published to ``md:health`` (future).

    Independent signals (DESIGN-REVIEW-2 §20): the API being up never implies market-data
    health. ``market_data_age_seconds`` is the freshness signal; ``universe_sync`` reports
    producer/consumer universe-version agreement.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    producer_id: str = Field(min_length=1, max_length=128)
    producer_epoch: int = Field(ge=0)
    updated_at: datetime
    ingestion: ProviderStatus = ProviderStatus.UNKNOWN
    transport: ProviderStatus = ProviderStatus.UNKNOWN
    universe_sync: ProviderStatus = ProviderStatus.UNKNOWN
    market_data_age_seconds: float | None = Field(default=None, ge=0)
    last_event_at: datetime | None = None

    _validate_updated_at = field_validator("updated_at")(_require_aware)

    @field_validator("last_event_at")
    @classmethod
    def _validate_last_event_at(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value) if value is not None else None


def health_key(config: MarketIpcConfig) -> str:
    """Redis key for the ingestion health snapshot."""
    return config.health_key
