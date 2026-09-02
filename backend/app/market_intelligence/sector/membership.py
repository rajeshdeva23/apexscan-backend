"""Deterministic, read-only sector membership dataset and resolver (SECTOR-2).

No network, no DB, no Redis, no provider, no MarketContext. A dataset is validated
fail-closed on construction of its resolver; resolution is pure and effective-dated.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from app.market_intelligence.sector.models import (
    FrozenModel,
    GroupKind,
    InstrumentSectorProfile,
    SectorDatasetError,
    SectorDatasetMetadata,
    SectorDefinition,
    SectorMembership,
)

_PRIMARY_KINDS = frozenset({GroupKind.PRIMARY_SECTOR, GroupKind.INDUSTRY})
_INDEX_KINDS = frozenset(
    {GroupKind.SECTOR_INDEX, GroupKind.THEMATIC_INDEX, GroupKind.BROAD_MARKET_INDEX}
)


class SectorMembershipDataset(FrozenModel):
    """An immutable, provenance-bearing set of group definitions and memberships."""

    metadata: SectorDatasetMetadata
    sector_definitions: tuple[SectorDefinition, ...]
    index_definitions: tuple[SectorDefinition, ...]
    memberships: tuple[SectorMembership, ...]


def _validate_definitions(dataset: SectorMembershipDataset) -> dict[str, SectorDefinition]:
    defs: dict[str, SectorDefinition] = {}
    for definition in (*dataset.sector_definitions, *dataset.index_definitions):
        if definition.sector_id in defs:
            raise SectorDatasetError(f"duplicate sector_id: {definition.sector_id}")
        defs[definition.sector_id] = definition
    for definition in dataset.sector_definitions:
        if definition.kind not in _PRIMARY_KINDS:
            raise SectorDatasetError(f"non-primary kind on definition {definition.sector_id}")
    for definition in dataset.index_definitions:
        if definition.kind not in _INDEX_KINDS:
            raise SectorDatasetError(f"non-index kind on definition {definition.sector_id}")
    return defs


def _validate_memberships(
    dataset: SectorMembershipDataset, defs: dict[str, SectorDefinition]
) -> None:
    seen: set[tuple[str, str, date]] = set()
    for membership in dataset.memberships:
        definition = defs.get(membership.sector_id)
        if definition is None:
            raise SectorDatasetError(f"undefined sector in membership: {membership.sector_id}")
        if membership.primary and definition.kind not in _PRIMARY_KINDS:
            raise SectorDatasetError(f"primary flag on non-primary kind: {membership.sector_id}")
        key = (membership.identity, membership.sector_id, membership.effective_from)
        if key in seen:
            raise SectorDatasetError(f"duplicate membership row: {key}")
        seen.add(key)


def _validate_primary_intervals(dataset: SectorMembershipDataset) -> None:
    by_identity: dict[str, list[SectorMembership]] = defaultdict(list)
    for membership in dataset.memberships:
        if membership.primary:
            by_identity[membership.identity].append(membership)
    for identity, rows in by_identity.items():
        rows.sort(key=lambda m: m.effective_from)
        for earlier, later in zip(rows, rows[1:], strict=False):
            if earlier.effective_to is None or later.effective_from < earlier.effective_to:
                raise SectorDatasetError(f"overlapping active primary intervals for {identity}")


class MembershipResolver:
    """Read-only, in-memory, effective-dated resolver over a validated dataset."""

    def __init__(self, dataset: SectorMembershipDataset) -> None:
        """Validate the dataset fail-closed and build O(1)/O(k) lookup indexes."""
        self._defs = _validate_definitions(dataset)
        _validate_memberships(dataset, self._defs)
        _validate_primary_intervals(dataset)
        self._metadata = dataset.metadata
        self._by_identity: dict[str, tuple[SectorMembership, ...]] = {}
        grouped: dict[str, list[SectorMembership]] = defaultdict(list)
        for membership in dataset.memberships:
            grouped[membership.identity].append(membership)
        self._by_identity = {identity: tuple(rows) for identity, rows in grouped.items()}
        self._primary_sector_ids = tuple(sorted(d.sector_id for d in dataset.sector_definitions))

    def _on(self, on: date | None) -> date:
        return on if on is not None else self._metadata.effective_from

    def resolve_primary(self, identity: str, on: date | None = None) -> str | None:
        """Return the active primary sector id for ``identity``, or ``None`` if unmapped."""
        when = self._on(on)
        for membership in self._by_identity.get(identity, ()):  # noqa: SIM110 - explicit
            if membership.primary and membership.active_on(when):
                return membership.sector_id
        return None

    def secondary_memberships(self, identity: str, on: date | None = None) -> tuple[str, ...]:
        """Return active non-primary index memberships (overlapping, sorted)."""
        when = self._on(on)
        return tuple(
            sorted(
                m.sector_id
                for m in self._by_identity.get(identity, ())
                if not m.primary and m.active_on(when)
            )
        )

    def resolve_profile(self, identity: str, on: date | None = None) -> InstrumentSectorProfile:
        """Return the full resolved profile — never raises; unmapped -> primary None."""
        secondary = self.secondary_memberships(identity, on)
        broad = tuple(s for s in secondary if self._defs[s].kind is GroupKind.BROAD_MARKET_INDEX)
        non_broad = tuple(s for s in secondary if s not in broad)
        return InstrumentSectorProfile(
            identity=identity,
            primary_sector=self.resolve_primary(identity, on),
            secondary_memberships=non_broad,
            broad_market_memberships=broad,
        )

    def members_of_primary_sector(self, sector_id: str, on: date | None = None) -> tuple[str, ...]:
        """Return identities whose active primary sector is ``sector_id`` (sorted)."""
        when = self._on(on)
        return tuple(
            sorted(
                identity
                for identity, rows in self._by_identity.items()
                if any(m.primary and m.sector_id == sector_id and m.active_on(when) for m in rows)
            )
        )

    def all_primary_sectors(self) -> tuple[str, ...]:
        """Return every defined primary sector id (deterministic order)."""
        return self._primary_sector_ids
