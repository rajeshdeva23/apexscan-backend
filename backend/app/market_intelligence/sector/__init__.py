"""Sector taxonomy, membership, and deterministic resolution (SECTOR-2, ADR-016).

Reference/domain foundation only — no live metrics, scoring, ranking, EventBus
subscription, or I/O. Those arrive in SECTOR-3+ and depend on this contract.
"""

from app.market_intelligence.sector.membership import (
    MembershipResolver,
    SectorMembershipDataset,
)
from app.market_intelligence.sector.models import (
    GroupKind,
    InstrumentSectorProfile,
    SectorDatasetError,
    SectorDatasetMetadata,
    SectorDefinition,
    SectorMembership,
    instrument_identity,
)
from app.market_intelligence.sector.reference_data import load_sector_membership_dataset

__all__ = [
    "GroupKind",
    "InstrumentSectorProfile",
    "MembershipResolver",
    "SectorDatasetError",
    "SectorDatasetMetadata",
    "SectorDefinition",
    "SectorMembership",
    "SectorMembershipDataset",
    "instrument_identity",
    "load_sector_membership_dataset",
]
