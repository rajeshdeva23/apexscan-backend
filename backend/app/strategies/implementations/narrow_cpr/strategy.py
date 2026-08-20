"""Narrow CPR strategy — V1 (ADR-007 Narrow CPR strategy specification).

A non-directional compression/context feature over the **previous completed
authoritative trading session** (NCR1). It declares a single session
``HistoricalRequirement(lookback=1)`` (the mandatory cause that populates
``previous_session``; NCR13), reads ``context.historical.previous_session`` at
evaluate time, computes the pivot-normalised CPR width percentage via the pure
calculator, and emits it as structured metrics (NCR5/NCR10).

Invariants: pure and deterministic (no clock/network/random/global mutation; NCR19);
provider-neutral (reads only the broker-neutral ``MarketContext``; NCR20);
non-repainting (consumes only completed history, never a current-day field; NCR8);
non-directional (no BUY/SELL/bias; NCR12). ``score`` is left ``None`` in V1 — a
non-arbitrary bounded score requires the deferred historical percentile, and the
cross-instrument scanner (a later slice) ranks ascending by the ``cpr_width_pct``
metric (NCR11/NCR16/NCR23).
"""

from __future__ import annotations

from decimal import Decimal

from app.market_engine.context import MarketContext
from app.market_engine.historical.context import PreviousSessionFacts
from app.market_engine.historical.requirements import HistoricalRequirement
from app.market_engine.timeframe import Timeframe
from app.strategies.configuration import StrategyConfiguration
from app.strategies.contracts import StrategyEvaluationMetadata
from app.strategies.descriptor import StrategyDescriptor
from app.strategies.enums import (
    CandleCompleteness,
    EmissionPolicy,
    EvaluationStatus,
    FactNeed,
    StrategyCategory,
    StrategyTrigger,
)
from app.strategies.implementations.narrow_cpr.calculator import CprResult, compute_cpr
from app.strategies.implementations.narrow_cpr.configuration import NarrowCprConfiguration
from app.strategies.requirements import StrategyRequirements
from app.strategies.results import MetricEntry, StrategyEvaluation

_STRATEGY_ID = "narrow_cpr"
_VERSION = "1.0.0"

# Reason codes (structured explainability; docs/07 §12 — never English text).
_REASON_VALID = "NARROW_CPR_VALID"
_REASON_WITHIN = "NARROW_CPR_WITHIN_THRESHOLD"
_REASON_ABOVE = "NARROW_CPR_ABOVE_THRESHOLD"
_REASON_NO_PREVIOUS = "NARROW_CPR_NO_PREVIOUS_SESSION"

_DESCRIPTOR = StrategyDescriptor(
    strategy_id=_STRATEGY_ID,
    display_name="Narrow CPR",
    description=(
        "Ranks instruments by the narrowness of today's Central Pivot Range derived from "
        "the previous completed trading session. A non-directional compression/context "
        "feature — never a buy/sell signal."
    ),
    version=_VERSION,
    category=StrategyCategory.MARKET_STRUCTURE,
    emission_policy=EmissionPolicy.ONE_SHOT_PER_SESSION,
)

_REQUIREMENTS = StrategyRequirements(
    historical=(HistoricalRequirement(timeframe=Timeframe.session(), lookback=1),),
    fact_needs=(FactNeed.PREVIOUS_SESSION,),
    trigger=StrategyTrigger.ON_HISTORICAL_READY,
    candle_completeness=CandleCompleteness.AUTHORITATIVE_ONLY,
)


class NarrowCprStrategy:
    """The V1 Narrow CPR strategy (conforms to the ``Strategy`` protocol)."""

    @property
    def descriptor(self) -> StrategyDescriptor:
        """Return the immutable identity/metadata for this strategy."""
        return _DESCRIPTOR

    @property
    def requirements(self) -> StrategyRequirements:
        """Return the declared requirements: one session lookback, previous-session fact."""
        return _REQUIREMENTS

    @property
    def configuration_type(self) -> type[StrategyConfiguration]:
        """Return the concrete configuration type this strategy validates against."""
        return NarrowCprConfiguration

    def evaluate(
        self,
        context: MarketContext,
        configuration: StrategyConfiguration,
        metadata: StrategyEvaluationMetadata,
    ) -> StrategyEvaluation:
        """Evaluate the previous-session CPR into a non-directional narrowness feature.

        Reads only ``context.historical.previous_session`` (no current-day field). When
        the previous session is absent the evaluation is ``SKIPPED`` (fail-closed); this is
        only reachable on misuse before readiness, since the readiness gate blocks
        evaluation until warmup populates the previous session.
        """
        previous = self._previous_session(context)
        if previous is None:
            return StrategyEvaluation(
                instrument=context.instrument,
                context_version=context.version,
                status=EvaluationStatus.SKIPPED,
                reason_codes=(_REASON_NO_PREVIOUS,),
            )
        threshold = self._threshold(configuration)
        result = compute_cpr(
            previous.candle.high_price, previous.candle.low_price, previous.candle.close_price
        )
        status, reason = _classify(result, threshold)
        return StrategyEvaluation(
            instrument=context.instrument,
            context_version=context.version,
            status=status,
            score=None,
            reason_codes=(reason,),
            metrics=_metrics(result, previous),
        )

    @staticmethod
    def _previous_session(context: MarketContext) -> PreviousSessionFacts | None:
        """Return the authoritative previous-session facts, or ``None`` when absent."""
        if context.historical is None:
            return None
        return context.historical.previous_session

    @staticmethod
    def _threshold(configuration: StrategyConfiguration) -> Decimal | None:
        """Return the configured narrowness threshold, requiring the concrete config type."""
        if not isinstance(configuration, NarrowCprConfiguration):
            raise TypeError("NarrowCprStrategy requires a NarrowCprConfiguration")
        return configuration.narrow_cpr_max_width_pct


def _classify(result: CprResult, threshold: Decimal | None) -> tuple[EvaluationStatus, str]:
    """Map the CPR width against the optional threshold to a status + reason code."""
    if threshold is None:
        return EvaluationStatus.MATCHED, _REASON_VALID
    if result.cpr_width_pct <= threshold:
        return EvaluationStatus.MATCHED, _REASON_WITHIN
    return EvaluationStatus.NO_MATCH, _REASON_ABOVE


def _metrics(result: CprResult, previous: PreviousSessionFacts) -> tuple[MetricEntry, ...]:
    """Build the structured CPR metrics (Decimal geometry + authoritative source date)."""
    candle = previous.candle
    return (
        MetricEntry(name="cpr_width_pct", value=result.cpr_width_pct),
        MetricEntry(name="pivot", value=result.pivot),
        MetricEntry(name="bc", value=result.bc),
        MetricEntry(name="tc", value=result.tc),
        MetricEntry(name="cpr_top", value=result.cpr_top),
        MetricEntry(name="cpr_bottom", value=result.cpr_bottom),
        MetricEntry(name="cpr_width", value=result.cpr_width),
        MetricEntry(name="previous_high", value=candle.high_price),
        MetricEntry(name="previous_low", value=candle.low_price),
        MetricEntry(name="previous_close", value=candle.close_price),
        MetricEntry(name="source_session_date", value=previous.trading_date.isoformat()),
    )
