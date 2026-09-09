"""Immutable, versioned, broker-neutral UniverseSnapshot + deterministic diff (PHASE E).

A :class:`UniverseSnapshot` is the shared authority for "which instruments are in the effective
ApexScan live F&O universe on a trading date". It is immutable (a change produces a new
snapshot), deterministic (input order never affects content), and content-addressed by a SHA-256
over its canonical logical content (never over Python object identity / repr / timestamps).

Broker-neutral: instrument identity is the canonical ``EXCHANGE:SYMBOL`` (reusing the existing
``Instrument`` model and ``instrument_identity`` helper). ``provider_security_id`` /
``exchange_segment`` travel ONLY as reference mapping metadata for ingestion subscription — they
never enter canonical MarketData, strategy, scanner, sector, or trading logic.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.market_intelligence.sector.models import instrument_identity
from app.schemas.market_data import Instrument

SCHEMA_VERSION = 1
CANDIDATE_VERSION = 0  # sentinel universe_version for un-promoted candidates


class SnapshotState(StrEnum):
    """Governance state: a resolver produces a CANDIDATE; explicit promotion makes it PROMOTED."""

    CANDIDATE = "candidate"
    PROMOTED = "promoted"


class SourceProvenance(BaseModel):
    """Bounded, credential-free provenance of the inputs a snapshot was resolved from.

    Records source identifiers/versions/dates for audit — never raw payloads, URLs with tokens,
    or secrets.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    fno_source: str = Field(min_length=1, max_length=128)
    fno_version: str = Field(min_length=1, max_length=64)
    fno_effective_date: date
    instrument_master_source: str = Field(min_length=1, max_length=128)
    instrument_master_version: str = Field(min_length=1, max_length=64)
    sector_dataset_id: str = Field(min_length=1, max_length=128)
    sector_version: str = Field(min_length=1, max_length=64)

    def canonical_dict(self) -> dict[str, str]:
        """Deterministic string map used inside the content fingerprint."""
        return {
            "fno_source": self.fno_source,
            "fno_version": self.fno_version,
            "fno_effective_date": self.fno_effective_date.isoformat(),
            "instrument_master_source": self.instrument_master_source,
            "instrument_master_version": self.instrument_master_version,
            "sector_dataset_id": self.sector_dataset_id,
            "sector_version": self.sector_version,
        }


class UniverseInstrument(BaseModel):
    """One resolved live-universe member: canonical instrument + provider mapping + sector."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    instrument: Instrument
    provider_security_id: str = Field(min_length=1, max_length=64)
    exchange_segment: str = Field(min_length=1, max_length=32)
    sector: str | None = Field(default=None, min_length=1, max_length=64)  # None = UNCLASSIFIED

    @property
    def identity(self) -> str:
        """Broker-neutral canonical identity (``EXCHANGE:SYMBOL``)."""
        return instrument_identity(self.instrument)


class UniverseSnapshot(BaseModel):
    """Immutable, versioned, content-addressed effective F&O universe for one trading date."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    schema_version: int
    universe_version: int = Field(ge=0)
    trading_date: date  # the effective trading date this snapshot applies to
    effective_at: datetime
    state: SnapshotState
    instruments: tuple[UniverseInstrument, ...]
    source_provenance: SourceProvenance
    content_sha256: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def _validate_sorted_unique_and_hash(self) -> UniverseSnapshot:
        identities = [i.identity for i in self.instruments]
        if identities != sorted(identities):
            raise ValueError("universe instruments must be sorted by identity")
        if len(identities) != len(set(identities)):
            raise ValueError("universe instruments must have unique identities")
        expected = content_sha256(self.trading_date, self.instruments, self.source_provenance)
        if self.content_sha256 != expected:
            raise ValueError("content_sha256 does not match snapshot content")
        return self

    @property
    def sector_membership(self) -> dict[str, str | None]:
        """Derived ``identity -> sector`` map (``None`` = unclassified)."""
        return {i.identity: i.sector for i in self.instruments}

    @property
    def identities(self) -> tuple[str, ...]:
        """Sorted broker-neutral identities in this snapshot."""
        return tuple(i.identity for i in self.instruments)

    def promoted_as(self, universe_version: int, effective_at: datetime) -> UniverseSnapshot:
        """Return a PROMOTED copy carrying a real monotonic version (content hash unchanged)."""
        return self.model_copy(
            update={
                "universe_version": universe_version,
                "state": SnapshotState.PROMOTED,
                "effective_at": effective_at,
            }
        )


def _canonical_content(
    trading_date: date,
    instruments: tuple[UniverseInstrument, ...],
    provenance: SourceProvenance,
) -> str:
    """Deterministic canonical JSON of the LOGICAL content (order-independent).

    Excludes version/state/effective_at/hash so the same logical universe always fingerprints
    identically regardless of input order or which version it was promoted as.
    """
    payload = {
        "trading_date": trading_date.isoformat(),
        "instruments": [
            {
                "identity": item.identity,
                "provider_security_id": item.provider_security_id,
                "exchange_segment": item.exchange_segment,
                "sector": item.sector,
            }
            for item in sorted(instruments, key=lambda entry: entry.identity)
        ],
        "provenance": provenance.canonical_dict(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_sha256(
    trading_date: date,
    instruments: tuple[UniverseInstrument, ...],
    provenance: SourceProvenance,
) -> str:
    """SHA-256 hex digest over the canonical logical content."""
    canonical = _canonical_content(trading_date, instruments, provenance)
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_snapshot(
    *,
    universe_version: int,
    trading_date: date,
    effective_at: datetime,
    state: SnapshotState,
    instruments: tuple[UniverseInstrument, ...],
    provenance: SourceProvenance,
) -> UniverseSnapshot:
    """Construct a snapshot with instruments canonically sorted and the content hash computed."""
    ordered = tuple(sorted(instruments, key=lambda entry: entry.identity))
    return UniverseSnapshot(
        schema_version=SCHEMA_VERSION,
        universe_version=universe_version,
        trading_date=trading_date,
        effective_at=effective_at,
        state=state,
        instruments=ordered,
        source_provenance=provenance,
        content_sha256=content_sha256(trading_date, ordered, provenance),
    )


class UniverseDiff(BaseModel):
    """Deterministic difference between two snapshots (all identity tuples sorted)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    from_version: int
    to_version: int
    added: tuple[str, ...]
    removed: tuple[str, ...]
    unchanged: tuple[str, ...]
    mapping_changed: tuple[str, ...]
    sector_changed: tuple[str, ...]


def diff_snapshots(old: UniverseSnapshot, new: UniverseSnapshot) -> UniverseDiff:
    """Compute the deterministic membership/mapping/sector diff from ``old`` to ``new``.

    A symbol/identity change surfaces as a removed(old identity) + added(new identity) pair;
    correlating them requires corporate-action rules deferred to Phase F.
    """
    old_by_id = {item.identity: item for item in old.instruments}
    new_by_id = {item.identity: item for item in new.instruments}
    old_ids, new_ids = set(old_by_id), set(new_by_id)
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)
    common = new_ids & old_ids
    mapping_changed, sector_changed, unchanged = [], [], []
    for identity in sorted(common):
        before, after = old_by_id[identity], new_by_id[identity]
        mapping_diff = (before.provider_security_id, before.exchange_segment) != (
            after.provider_security_id,
            after.exchange_segment,
        )
        sector_diff = before.sector != after.sector
        if mapping_diff:
            mapping_changed.append(identity)
        if sector_diff:
            sector_changed.append(identity)
        if not mapping_diff and not sector_diff:
            unchanged.append(identity)
    return UniverseDiff(
        from_version=old.universe_version,
        to_version=new.universe_version,
        added=tuple(added),
        removed=tuple(removed),
        unchanged=tuple(unchanged),
        mapping_changed=tuple(mapping_changed),
        sector_changed=tuple(sector_changed),
    )
