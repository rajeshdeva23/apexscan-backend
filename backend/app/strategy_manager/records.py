"""Immutable manager-owned evaluation record and readiness verdict (P5.3).

The manager wraps each strategy's outcome with the identity, trigger, and readiness
that produced it — for internal observability only. This is neither a ranked result
nor a persisted ``StrategyResult`` (those are P5.5); it carries no rank and never
mutates the underlying :class:`StrategyEvaluation`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.strategies.enums import StrategyTrigger
from app.strategies.results import StrategyEvaluation


class Readiness(StrEnum):
    """Whether a running, trigger-matched strategy could be evaluated this cycle."""

    READY = "ready"
    MISSING_FACTS = "missing_facts"
    MISSING_HISTORICAL = "missing_historical"
    MISSING_LIVE_TIMEFRAME = "missing_live_timeframe"
    INCOMPATIBLE_CONTEXT = "incompatible_context"
    MISSING_CONFIGURATION = "missing_configuration"
    MISSING_SESSION_STATISTICS = "missing_session_statistics"
    SESSION_STATISTICS_NOT_AUTHORITATIVE = "session_statistics_not_authoritative"
    SESSION_STATISTICS_STALE = "session_statistics_stale"


@dataclass(frozen=True, slots=True)
class StrategyEvaluationRecord:
    """One strategy's outcome for one MarketContext, with its identity and gating.

    Attributes:
        strategy_id: The strategy that produced the evaluation.
        trigger: The trigger under which it was considered.
        readiness: Whether it was ready (``READY``) or why it was skipped.
        evaluation: The strategy's evaluation, or a manager-built SKIPPED/ERROR one.
    """

    strategy_id: str
    trigger: StrategyTrigger
    readiness: Readiness
    evaluation: StrategyEvaluation
