"""Planning-time readiness requirement + adapter from existing StrategyRequirements (PHASE G).

The engine checks whether declared inputs EXIST; it never knows strategy logic. Rather than invent
a second requirement authority, :func:`readiness_requirement_from` projects the existing
``StrategyRequirements`` (the strategy's own declaration) into a broker-neutral
:class:`ReadinessRequirement`, failing closed on any declared need it cannot project (so a
strategy is never falsely marked planning-ready for an unverifiable requirement).
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

from app.market_history import PriceAdjustment
from app.strategies.enums import FactNeed
from app.strategies.requirements import StrategyRequirements

# FactNeeds this planning layer can project to a data-availability check. LATEST_QUOTE and
# SESSION_STATISTICS are governed by the live evaluation-time gate, not planning foundations.
_PROJECTABLE_FACT_NEEDS = frozenset(
    {FactNeed.PREVIOUS_SESSION, FactNeed.SESSION, FactNeed.LATEST_TICK}
)


class UnsupportedReadinessRequirementError(ValueError):
    """Raised when a strategy declares a need the planning readiness layer cannot project."""


class HistoricalNeed(BaseModel):
    """A historical-completeness need: N trading days at a given adjustment."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    trading_days: int = Field(ge=1)
    adjustment: PriceAdjustment = PriceAdjustment.RAW


class ReadinessRequirement(BaseModel):
    """Broker-neutral declaration of the data inputs a strategy needs to be planning-ready."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    historical: HistoricalNeed | None = None
    needs_previous_day_ohlc: bool = False
    needs_previous_close: bool = False
    needs_session_open: bool = False
    needs_last_price: bool = False
    max_live_age: timedelta | None = Field(default=None, gt=timedelta(0))

    @property
    def requires_live(self) -> bool:
        """Whether any current-session live field is required."""
        return self.needs_session_open or self.needs_last_price


def readiness_requirement_from(
    requirements: StrategyRequirements, *, needs_previous_close: bool = False
) -> ReadinessRequirement:
    """Project an existing ``StrategyRequirements`` into a planning :class:`ReadinessRequirement`.

    Fails closed (:class:`UnsupportedReadinessRequirementError`) on any fact need outside the
    projectable set. ``previous_close`` is not part of the current fact vocabulary, so callers
    declare it explicitly via ``needs_previous_close``.
    """
    unsupported = sorted(
        f.value for f in requirements.fact_needs if f not in _PROJECTABLE_FACT_NEEDS
    )
    if unsupported:
        raise UnsupportedReadinessRequirementError(
            f"cannot project fact needs into planning readiness: {unsupported}"
        )
    historical: HistoricalNeed | None = None
    if requirements.historical:
        historical = HistoricalNeed(
            trading_days=max(entry.lookback for entry in requirements.historical)
        )
    strictest_age: timedelta | None = None
    if requirements.freshness:
        strictest_age = min(entry.max_age for entry in requirements.freshness)
    return ReadinessRequirement(
        historical=historical,
        needs_previous_day_ohlc=FactNeed.PREVIOUS_SESSION in requirements.fact_needs,
        needs_previous_close=needs_previous_close,
        needs_session_open=FactNeed.SESSION in requirements.fact_needs,
        needs_last_price=FactNeed.LATEST_TICK in requirements.fact_needs,
        max_live_age=strictest_age,
    )
