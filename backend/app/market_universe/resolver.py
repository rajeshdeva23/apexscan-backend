"""UniverseResolver: build a CANDIDATE snapshot from reference inputs (DECOUPLING PHASE E).

Governance (Phase-E decision): RESOLVE automatically, PROMOTE explicitly. The resolver joins the
F&O membership list to a broker-neutral provider mapping and the SECTOR-2 membership authority to
produce a deterministic CANDIDATE snapshot plus an explicit :class:`ResolutionResult` that never
silently drops a problem instrument — a missing provider mapping, a conflicting mapping, or an
empty universe is surfaced with a machine-readable reason. Promotion (:func:`promote`) fails
closed on any of those.

The core is broker-neutral: it depends on a :class:`ProviderMappingSource` and a
:class:`SectorAuthority` Protocol, never on the Dhan adapter.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.market_intelligence.sector.models import instrument_identity
from app.market_universe.snapshot import (
    CANDIDATE_VERSION,
    SnapshotState,
    SourceProvenance,
    UniverseInstrument,
    UniverseSnapshot,
    build_snapshot,
)
from app.schemas.market_data import Instrument


class UnresolvedReason(StrEnum):
    """Machine-readable reason a live-universe instrument could not be resolved."""

    MISSING_PROVIDER_MAPPING = "missing_provider_mapping"
    CONFLICTING_PROVIDER_MAPPING = "conflicting_provider_mapping"


class ProviderMapping(BaseModel):
    """Broker-neutral provider subscription mapping for one instrument (reference metadata)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    provider_security_id: str = Field(min_length=1, max_length=64)
    exchange_segment: str = Field(min_length=1, max_length=32)


@runtime_checkable
class ProviderMappingSource(Protocol):
    """Supplies the provider subscription mapping for a canonical instrument (None = unmapped)."""

    def mapping_for(self, instrument: Instrument) -> ProviderMapping | None:
        """Return the provider mapping for ``instrument`` or None when unavailable."""
        ...


@runtime_checkable
class SectorAuthority(Protocol):
    """Date-effective primary-sector authority (satisfied by SECTOR-2 ``MembershipResolver``)."""

    def resolve_primary(self, identity: str, on: date | None = None) -> str | None:
        """Return the primary sector id for ``identity`` on ``on`` (None = unclassified)."""
        ...


class UnresolvedInstrument(BaseModel):
    """An instrument that could not enter the snapshot, with an explicit reason."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    identity: str
    reason: UnresolvedReason


class ResolutionResult(BaseModel):
    """Outcome of one resolution: the candidate plus explicit unresolved/conflict surfaces."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    candidate: UniverseSnapshot
    unresolved: tuple[UnresolvedInstrument, ...]
    unclassified_sector: tuple[str, ...]

    @property
    def is_promotable(self) -> bool:
        """Whether the candidate has no unresolved instruments and is non-empty."""
        return not self.unresolved and bool(self.candidate.instruments)


class EmptyUniverseError(ValueError):
    """Raised when promoting a candidate whose resolved universe is empty (fail-closed)."""


class PromotionValidationError(ValueError):
    """Raised when a candidate with unresolved/conflicting mappings is promoted (fail-closed)."""


class UniverseResolver:
    """Builds deterministic CANDIDATE snapshots from broker-neutral reference inputs."""

    def __init__(
        self,
        *,
        mapping_source: ProviderMappingSource,
        sector_authority: SectorAuthority,
    ) -> None:
        self._mapping_source = mapping_source
        self._sector_authority = sector_authority

    def resolve(
        self,
        membership: Iterable[Instrument],
        *,
        trading_date: date,
        effective_at: datetime,
        provenance: SourceProvenance,
    ) -> ResolutionResult:
        """Resolve the F&O ``membership`` into a candidate snapshot for ``trading_date``.

        A missing provider mapping or a provider-security-id shared by two identities is recorded
        as unresolved (never silently dropped). Unclassified sectors are recorded but do not block
        the candidate. Input order does not affect the result.
        """
        resolved, unresolved, unclassified = self._resolve_instruments(membership, trading_date)
        candidate = build_snapshot(
            universe_version=CANDIDATE_VERSION,
            trading_date=trading_date,
            effective_at=effective_at,
            state=SnapshotState.CANDIDATE,
            instruments=tuple(resolved),
            provenance=provenance,
        )
        return ResolutionResult(
            candidate=candidate,
            unresolved=tuple(sorted(unresolved, key=lambda entry: entry.identity)),
            unclassified_sector=tuple(sorted(unclassified)),
        )

    def _resolve_instruments(
        self, membership: Iterable[Instrument], trading_date: date
    ) -> tuple[list[UniverseInstrument], list[UnresolvedInstrument], list[str]]:
        """Join each instrument to its provider mapping + sector; classify unresolved/conflicts."""
        mapped: dict[str, tuple[Instrument, ProviderMapping]] = {}
        unresolved: list[UnresolvedInstrument] = []
        for instrument in membership:
            identity = instrument_identity(instrument)
            mapping = self._mapping_source.mapping_for(instrument)
            if mapping is None:
                unresolved.append(
                    UnresolvedInstrument(
                        identity=identity, reason=UnresolvedReason.MISSING_PROVIDER_MAPPING
                    )
                )
                continue
            mapped[identity] = (instrument, mapping)
        conflicts = _security_id_conflicts(mapped)
        resolved: list[UniverseInstrument] = []
        unclassified: list[str] = []
        for identity, (instrument, mapping) in mapped.items():
            if identity in conflicts:
                unresolved.append(
                    UnresolvedInstrument(
                        identity=identity, reason=UnresolvedReason.CONFLICTING_PROVIDER_MAPPING
                    )
                )
                continue
            sector = self._sector_authority.resolve_primary(identity, on=trading_date)
            if sector is None:
                unclassified.append(identity)
            resolved.append(
                UniverseInstrument(
                    instrument=instrument,
                    provider_security_id=mapping.provider_security_id,
                    exchange_segment=mapping.exchange_segment,
                    sector=sector,
                )
            )
        return resolved, unresolved, unclassified


def _security_id_conflicts(
    mapped: dict[str, tuple[Instrument, ProviderMapping]],
) -> set[str]:
    """Return identities whose provider_security_id is shared by another identity (ambiguous)."""
    by_security_id: dict[str, list[str]] = {}
    for identity, (_instrument, mapping) in mapped.items():
        by_security_id.setdefault(mapping.provider_security_id, []).append(identity)
    conflicts: set[str] = set()
    for identities in by_security_id.values():
        if len(identities) > 1:
            conflicts.update(identities)
    return conflicts


def validate_promotable(result: ResolutionResult) -> None:
    """Fail closed unless the candidate is fully resolved and non-empty (governance gate)."""
    if result.unresolved:
        raise PromotionValidationError(
            f"candidate has {len(result.unresolved)} unresolved instrument(s); promotion blocked"
        )
    if not result.candidate.instruments:
        raise EmptyUniverseError("refusing to promote an empty universe (suspected source failure)")


class InMemoryProviderMappingSource:
    """Reference/test provider-mapping source backed by an in-memory dict (broker-neutral)."""

    def __init__(self, mappings: dict[Instrument, ProviderMapping]) -> None:
        self._mappings = dict(mappings)

    def mapping_for(self, instrument: Instrument) -> ProviderMapping | None:
        """Return the configured mapping for ``instrument`` or None."""
        return self._mappings.get(instrument)
