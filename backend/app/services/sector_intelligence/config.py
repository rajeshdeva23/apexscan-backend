"""Shadow-runtime configuration and the non-production calculation policy (SECTOR-VIEW-1B).

The policy here is **NON-PRODUCTION, UN-CALIBRATED, and for shadow observability only**. It
carries no predictive meaning: ``direction_epsilon`` and ``freshness_limit`` are operational
observation choices, not trading, calibration, or signal thresholds. Production calibration is
SECTOR-5C/5D and is deliberately out of scope here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app.market_intelligence.sector.metrics import CalculationPolicy

# Un-calibrated shadow direction epsilon: the minimum magnitude a return must exceed to count
# as directional. Not a signal threshold. Matches the SECTOR-VALIDATION-1 harness value so
# offline validation and shadow observation stay consistent.
_SHADOW_DIRECTION_EPSILON = Decimal("0.001")

# Un-calibrated shadow freshness: an observation older than this at evaluation time is stale
# and excluded (the SECTOR-3/4 rule applies it). Operational cadence only.
_SHADOW_FRESHNESS_LIMIT = timedelta(minutes=5)

# The single shared shadow policy. Reuses the SECTOR-3 CalculationPolicy verbatim — no second
# freshness or direction definition exists anywhere in the shadow runtime.
SHADOW_VALIDATION_POLICY = CalculationPolicy(
    direction_epsilon=_SHADOW_DIRECTION_EPSILON,
    freshness_limit=_SHADOW_FRESHNESS_LIMIT,
)


@dataclass(frozen=True, slots=True)
class ShadowRuntimeConfig:
    """Bounded, validated configuration for the passive sector shadow runtime.

    Attributes:
        interval_seconds: Periodic evaluation cadence (operational only, not a threshold).
        policy: The un-calibrated SECTOR-3/4 calculation policy for shadow evaluation.
    """

    interval_seconds: float
    policy: CalculationPolicy = SHADOW_VALIDATION_POLICY

    def __post_init__(self) -> None:
        """Validate the cadence bound (fail fast on a nonsensical interval)."""
        if not 0 < self.interval_seconds <= 3600:
            raise ValueError(
                f"sector_shadow_interval_seconds must be in (0, 3600]; got {self.interval_seconds}"
            )
