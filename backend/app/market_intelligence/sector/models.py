"""Immutable domain vocabulary for sector taxonomy and membership (SECTOR-2).

These are reference/domain objects only — no live metrics, no scoring, no EventBus, no
I/O. The sector context defines its own frozen base rather than importing
``app.strategies.models.FrozenModel`` so it never depends on the strategy layer
(ADR-016 dependency direction).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from app.schemas.market_data import Instrument

_SECTOR_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True)


class FrozenModel(BaseModel):
    """Strict, immutable, extra-forbidding base for sector value objects."""

    model_config = _SECTOR_MODEL_CONFIG


class SectorDatasetError(ValueError):
    """A sector membership dataset violates a structural invariant (fail-closed)."""


class GroupKind(StrEnum):
    """The kind of group a definition represents. Never mixed silently.

    ``PRIMARY_SECTOR`` is the one authoritative, mutually-exclusive classification the
    V1 engine operates on. The remaining kinds are overlapping context/metadata only.
    """

    PRIMARY_SECTOR = "primary_sector"
    INDUSTRY = "industry"
    SECTOR_INDEX = "sector_index"
    THEMATIC_INDEX = "thematic_index"
    BROAD_MARKET_INDEX = "broad_market_index"


def instrument_identity(instrument: Instrument) -> str:
    """Return the canonical membership key for an instrument (``"NSE:RELIANCE"``).

    Matches the exchange:symbol identity MarketContext/observer already use, so
    membership joins deterministically to live events without a lookup table.
    """
    return f"{instrument.exchange}:{instrument.symbol}"


class SectorDefinition(FrozenModel):
    """An authoritative group definition (primary sector or index)."""

    sector_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: GroupKind
    source: str = Field(min_length=1)
    parent_id: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None

    @field_validator("sector_id")
    @classmethod
    def _no_whitespace_id(cls, value: str) -> str:
        if value.strip() != value or not value.strip():
            raise ValueError("sector_id must be a non-empty, untrimmed-whitespace key")
        return value


class SectorMembership(FrozenModel):
    """An effective-dated membership of one instrument in one group."""

    identity: str = Field(min_length=1)
    sector_id: str = Field(min_length=1)
    primary: bool
    effective_from: date
    source: str = Field(min_length=1)
    effective_to: date | None = None
    company: str | None = None
    isin: str | None = None

    @field_validator("effective_to")
    @classmethod
    def _interval_ordered(cls, value: date | None, info: ValidationInfo) -> date | None:
        start = info.data.get("effective_from")
        if value is not None and start is not None and value <= start:
            raise ValueError("effective_to must be strictly after effective_from")
        return value

    def active_on(self, on: date) -> bool:
        """Whether this membership is active on ``on`` (half-open [from, to))."""
        if on < self.effective_from:
            return False
        return self.effective_to is None or on < self.effective_to


class SectorDatasetMetadata(FrozenModel):
    """Provenance and version for a membership dataset (governed, reproducible)."""

    dataset_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    effective_from: date
    generated_at: date
    source_authority: str = Field(min_length=1)
    notes: str | None = None


class InstrumentSectorProfile(FrozenModel):
    """Resolved view of one instrument's primary sector and overlapping memberships.

    ``primary_sector`` is ``None`` when the instrument is unmapped — an explicit
    fail-closed status, never guessed or silently treated as neutral.
    """

    identity: str = Field(min_length=1)
    primary_sector: str | None
    secondary_memberships: tuple[str, ...] = ()
    broad_market_memberships: tuple[str, ...] = ()

    @property
    def is_mapped(self) -> bool:
        """Whether the instrument has an authoritative primary classification."""
        return self.primary_sector is not None
