"""Strategy contract layer — the immutable, broker-neutral vocabulary (P5.1).

Home for the shared Strategy contract and its value objects (descriptor,
requirements, configuration, evaluation/result, and enums). Every strategy and the
future Strategy Manager depend on this contract, never on each other's internals
(docs/07 §7; ADR-007). No orchestration, registry, lifecycle, or concrete strategy
lives here — those arrive in later Phase-5 slices. Individual strategy plug-ins
(Open=High, Open=Low, Narrow CPR, …) are separate, later artifacts.
"""

from app.strategies.configuration import StrategyConfiguration
from app.strategies.contracts import Strategy, StrategyEvaluationMetadata
from app.strategies.descriptor import StrategyDescriptor
from app.strategies.enums import (
    CandleCompleteness,
    EmissionPolicy,
    EvaluationStatus,
    FactNeed,
    StrategyCategory,
    StrategyLifecycleState,
    StrategyTrigger,
)
from app.strategies.errors import (
    InvalidStrategyError,
    StrategyAlreadyRegisteredError,
    StrategyNotFoundError,
    StrategyRegistryError,
)
from app.strategies.registry import StrategyRegistry
from app.strategies.requirements import FactFreshnessRequirement, StrategyRequirements
from app.strategies.results import MetricEntry, StrategyEvaluation, StrategyResult

__all__ = [
    "CandleCompleteness",
    "EmissionPolicy",
    "EvaluationStatus",
    "FactFreshnessRequirement",
    "FactNeed",
    "InvalidStrategyError",
    "MetricEntry",
    "Strategy",
    "StrategyAlreadyRegisteredError",
    "StrategyCategory",
    "StrategyConfiguration",
    "StrategyDescriptor",
    "StrategyEvaluation",
    "StrategyEvaluationMetadata",
    "StrategyLifecycleState",
    "StrategyNotFoundError",
    "StrategyRegistry",
    "StrategyRegistryError",
    "StrategyRequirements",
    "StrategyResult",
    "StrategyTrigger",
]
