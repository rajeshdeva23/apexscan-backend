"""Pure sector metrics engine (SECTOR-3, ADR-016).

Deterministic, side-effect-free math over constituent observations: return decomposition,
breadth, robust central tendency and dispersion, directional agreement, participation, an
F&O-universe benchmark proxy, relative strength, coverage, and an un-calibrated raw
direction. No SectorScore, confidence, thresholds, stock ranking, or live wiring.
"""

from app.market_intelligence.sector.metrics.engine import (
    calculate_constituent_metrics,
    calculate_relative_strength,
    calculate_sector_metrics,
    calculate_universe_proxy,
)
from app.market_intelligence.sector.metrics.models import (
    CalculationPolicy,
    ConstituentDirection,
    ConstituentMetrics,
    ConstituentObservation,
    DuplicateConstituentError,
    InvalidCalculationPolicyError,
    InvalidConstituentObservationError,
    MixedTradingDateError,
    RawSectorDirection,
    SectorBreadth,
    SectorDispersion,
    SectorMembershipMismatchError,
    SectorMetrics,
    SectorMetricsError,
    UniverseProxyMetrics,
)

__all__ = [
    "CalculationPolicy",
    "ConstituentDirection",
    "ConstituentMetrics",
    "ConstituentObservation",
    "DuplicateConstituentError",
    "InvalidCalculationPolicyError",
    "InvalidConstituentObservationError",
    "MixedTradingDateError",
    "RawSectorDirection",
    "SectorBreadth",
    "SectorDispersion",
    "SectorMembershipMismatchError",
    "SectorMetrics",
    "SectorMetricsError",
    "UniverseProxyMetrics",
    "calculate_constituent_metrics",
    "calculate_relative_strength",
    "calculate_sector_metrics",
    "calculate_universe_proxy",
]
