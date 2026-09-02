"""Immutable inputs, policy, enums, errors, and outputs for the pure metrics engine.

All values are un-calibrated raw evidence. No SectorScore, no confidence, no
strong/weak thresholds, no stock ranking — those are SECTOR-4/5. Prices and returns are
:class:`decimal.Decimal`; returns are **ratios** (``0.01`` == 1%), never percentages.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, ValidationInfo, field_validator

from app.market_intelligence.sector.models import FrozenModel


class SectorMetricsError(ValueError):
    """Base for pure-metrics domain errors (fail-closed)."""


class InvalidConstituentObservationError(SectorMetricsError):
    """A constituent observation is mathematically or structurally invalid."""


class DuplicateConstituentError(SectorMetricsError):
    """The same instrument identity appears more than once in one calculation."""


class MixedTradingDateError(SectorMetricsError):
    """Observations span more than one trading date (no cross-date blending)."""


class SectorMembershipMismatchError(SectorMetricsError):
    """An observation's sector_id disagrees with the sector being calculated."""


class InvalidCalculationPolicyError(SectorMetricsError):
    """A calculation policy value is out of range."""


class ConstituentDirection(StrEnum):
    """Intraday (since-open) direction of one constituent, gated by direction epsilon."""

    ADVANCING = "advancing"
    UNCHANGED = "unchanged"
    DECLINING = "declining"


class RawSectorDirection(StrEnum):
    """Un-calibrated raw sector direction (NOT the SECTOR-5 strong/weak classification).

    A directional call requires median-tilt and breadth-tilt to agree; any disagreement
    (including neutral-vs-directional) is ``MIXED``; both neutral is ``NEUTRAL``.
    """

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    MIXED = "mixed"
    INSUFFICIENT_DATA = "insufficient_data"


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


class CalculationPolicy(FrozenModel):
    """Explicit, UN-CALIBRATED calculation inputs — never hidden production constants.

    ``direction_epsilon`` and ``freshness_limit`` are calibration choices with no default
    (the caller must supply them). ``minimum_valid_count`` / ``minimum_coverage_ratio`` are
    structural sufficiency gates (defaults are the mathematical minimum, not trading
    thresholds). SECTOR-5 calibrates real values.
    """

    direction_epsilon: Decimal = Field(ge=0)
    freshness_limit: timedelta
    minimum_valid_count: int = Field(default=1, ge=1)
    minimum_coverage_ratio: Decimal | None = Field(default=None, ge=0, le=1)

    @field_validator("freshness_limit")
    @classmethod
    def _positive_freshness(cls, value: timedelta) -> timedelta:
        if value <= timedelta(0):
            raise InvalidCalculationPolicyError("freshness_limit must be positive")
        return value


class ConstituentObservation(FrozenModel):
    """One constituent's since-open snapshot. Non-positive prices are rejected here."""

    identity: str = Field(min_length=1)
    sector_id: str = Field(min_length=1)
    trading_date: date
    observation_timestamp: datetime
    last_price: Decimal = Field(gt=0)
    previous_close: Decimal = Field(gt=0)
    session_open: Decimal = Field(gt=0)

    @field_validator("observation_timestamp")
    @classmethod
    def _aware(cls, value: datetime, info: ValidationInfo) -> datetime:
        return _require_aware(value)


class ConstituentMetrics(FrozenModel):
    """Per-constituent return decomposition and direction (reused by SECTOR-4)."""

    identity: str
    sector_id: str
    overnight_return: Decimal
    intraday_return: Decimal
    total_return: Decimal
    direction: ConstituentDirection


class SectorBreadth(FrozenModel):
    """Equal-weight breadth counts and ratios over valid constituents."""

    valid_count: int
    advance_count: int
    decline_count: int
    unchanged_count: int
    advance_ratio: Decimal | None
    decline_ratio: Decimal | None
    unchanged_ratio: Decimal | None
    net_breadth: Decimal | None


class SectorDispersion(FrozenModel):
    """Robust dispersion of intraday returns (None where mathematically unavailable)."""

    mad_intraday_return: Decimal | None
    iqr_intraday_return: Decimal | None


class UniverseProxyMetrics(FrozenModel):
    """V1 benchmark proxy: equal-weight median intraday return of eligible F&O names.

    This is an F&O-universe proxy, NOT a NIFTY index return.
    """

    valid_count: int
    median_intraday_return: Decimal | None


class SectorMetrics(FrozenModel):
    """Complete raw evidence for one sector at one evaluation instant (un-calibrated)."""

    sector_id: str
    trading_date: date
    evaluation_timestamp: datetime
    expected_count: int
    valid_count: int
    stale_count: int
    missing_count: int
    invalid_count: int
    coverage_ratio: Decimal
    breadth: SectorBreadth
    median_overnight_return: Decimal | None
    median_intraday_return: Decimal | None
    median_total_return: Decimal | None
    dispersion: SectorDispersion
    directional_agreement: Decimal | None
    directional_participant_count: int
    directional_participation_ratio: Decimal | None
    raw_direction: RawSectorDirection
    universe_proxy_intraday_return: Decimal | None
    relative_strength: Decimal | None
    constituents: tuple[ConstituentMetrics, ...]
