"""Per-(instrument, strategy) planning-time readiness engine (DECOUPLING PHASE G).

Answers, for each (instrument, strategy): are all declared inputs available (universe membership,
historical completeness, reference + live data, freshness) before that strategy may consider the
instrument? Reuses the strategy's own ``StrategyRequirements`` (via a projecting adapter), Phase-F
``HistoricalCoverage``, Phase-E universe membership, Phase-D/live reference state, and the existing
``Clock``. Fails closed (never READY on a missing/stale/mismatched/unverifiable input) and there is
no global stock-ready flag. Capability only — production strategy routing is unchanged.
"""

from __future__ import annotations

from app.strategy_readiness.diagnostics import ReadinessDiagnostics, ReadinessMetrics
from app.strategy_readiness.engine import (
    HistoricalCoverageView,
    LiveReadinessState,
    StrategyReadinessEngine,
    StrategyReadinessGate,
    UniverseView,
)
from app.strategy_readiness.models import (
    ReadinessReason,
    ReadinessReasonCode,
    StrategyReadinessResult,
    StrategyReadinessSnapshot,
    StrategyReadinessStatus,
)
from app.strategy_readiness.requirements import (
    HistoricalNeed,
    ReadinessRequirement,
    UnsupportedReadinessRequirementError,
    readiness_requirement_from,
)

__all__ = [
    "HistoricalCoverageView",
    "HistoricalNeed",
    "LiveReadinessState",
    "ReadinessDiagnostics",
    "ReadinessMetrics",
    "ReadinessReason",
    "ReadinessReasonCode",
    "ReadinessRequirement",
    "StrategyReadinessEngine",
    "StrategyReadinessGate",
    "StrategyReadinessResult",
    "StrategyReadinessSnapshot",
    "StrategyReadinessStatus",
    "UniverseView",
    "UnsupportedReadinessRequirementError",
    "readiness_requirement_from",
]
