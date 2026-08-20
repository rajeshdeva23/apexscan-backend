"""Strategy Manager layer — lifecycle, evaluation routing, and requirement orchestration.

Owns the runtime strategy lifecycle FSM (P5.2, ADR-007), the evaluation router
(P5.3), the requirement-provisioning bridge (P5.4), and result promotion, emission
dedup, ranking, and publication (P5.5): the historical/live requirement registries,
the additive live-timeframe seam, historical warmup, and the StrategyResultsPublished
event carrying the ranked, deduplicated result set.
"""

from app.strategy_manager.dedup import EmissionDeduplicator
from app.strategy_manager.errors import (
    InvalidStrategyTransitionError,
    StrategyAlreadyTrackedError,
    StrategyLifecycleError,
    StrategyNotTrackedError,
)
from app.strategy_manager.events import StrategyResultsPublished
from app.strategy_manager.fact_requirements import FactRequirementRegistry
from app.strategy_manager.lifecycle import StrategyLifecycle
from app.strategy_manager.live_timeframes import LiveTimeframeRequirementRegistry
from app.strategy_manager.manager import StrategyManager
from app.strategy_manager.ports import (
    HistoricalWarmupPort,
    LiveTimeframeSink,
    SessionStatisticsRefreshControl,
)
from app.strategy_manager.promotion import promote_evaluation
from app.strategy_manager.ranking import RankedStrategyResult, rank_results
from app.strategy_manager.records import Readiness, StrategyEvaluationRecord
from app.strategy_manager.requirements_bridge import RequirementsCoordinator
from app.strategy_manager.status import StrategyRuntimeStatus

__all__ = [
    "EmissionDeduplicator",
    "FactRequirementRegistry",
    "HistoricalWarmupPort",
    "InvalidStrategyTransitionError",
    "LiveTimeframeRequirementRegistry",
    "LiveTimeframeSink",
    "RankedStrategyResult",
    "SessionStatisticsRefreshControl",
    "Readiness",
    "RequirementsCoordinator",
    "StrategyAlreadyTrackedError",
    "StrategyEvaluationRecord",
    "StrategyLifecycle",
    "StrategyLifecycleError",
    "StrategyManager",
    "StrategyNotTrackedError",
    "StrategyResultsPublished",
    "StrategyRuntimeStatus",
    "promote_evaluation",
    "rank_results",
]
