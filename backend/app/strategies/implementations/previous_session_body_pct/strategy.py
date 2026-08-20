"""Previous Session Body % strategy — V1 (ADR-007 PSB strategy specification).

A non-directional body-magnitude feature over the **previous completed authoritative
trading session** (PSB1). It declares a single session
``HistoricalRequirement(lookback=1)`` plus ``FactNeed.PREVIOUS_SESSION`` (PSB10/PSB11),
reads ``context.historical.previous_session`` at evaluate time, and computes the
open-normalised absolute body percentage via the pure calculator (PSB2).

Invariants: pure and deterministic (no clock/network/random/global mutation);
provider-neutral (reads only the broker-neutral ``MarketContext``; PSB24); non-repainting
(consumes only completed history, never a current-day field; PSB20); non-directional — the
absolute body treats an equal-magnitude up or down session identically (PSB4). ``score`` is
left ``None`` in V1 (PSB15); the cross-instrument scanner ranks **descending** by the
``previous_body_pct`` metric (PSB16) — largest absolute body ranks first. Rank-all: every
valid ready previous session MATCHES (PSB18).
"""

from __future__ import annotations

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
from app.strategies.implementations.previous_session_body_pct.calculator import (
    PreviousSessionBodyResult,
    compute_previous_session_body,
)
from app.strategies.implementations.previous_session_body_pct.configuration import (
    PreviousSessionBodyPctConfiguration,
)
from app.strategies.requirements import StrategyRequirements
from app.strategies.results import MetricEntry, StrategyEvaluation

_STRATEGY_ID = "previous_session_body_pct"
_VERSION = "1.0.0"

# Reason codes (structured explainability; docs/07 §12 — never English text).
_REASON_VALID = "PREVIOUS_SESSION_BODY_VALID"
_REASON_NO_PREVIOUS = "PREVIOUS_SESSION_BODY_NO_PREVIOUS"

_DESCRIPTOR = StrategyDescriptor(
    strategy_id=_STRATEGY_ID,
    display_name="Previous Session Body %",
    description=(
        "Ranks instruments by the absolute candle-body size of the previous completed "
        "trading session as a percentage of its open (largest body first). A non-directional "
        "magnitude feature — never a buy/sell signal."
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


class PreviousSessionBodyPctStrategy:
    """The V1 Previous Session Body % strategy (conforms to the ``Strategy`` protocol)."""

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
        return PreviousSessionBodyPctConfiguration

    def evaluate(
        self,
        context: MarketContext,
        configuration: StrategyConfiguration,
        metadata: StrategyEvaluationMetadata,
    ) -> StrategyEvaluation:
        """Evaluate the previous-session body into a non-directional magnitude feature.

        Reads only ``context.historical.previous_session`` (no current-day field). When
        the previous session is absent the evaluation is ``SKIPPED`` (fail-closed); this is
        only reachable on misuse before readiness, since the readiness gate blocks
        evaluation until warmup populates the previous session. Rank-all: a valid previous
        session always ``MATCHED`` (PSB18).
        """
        if not isinstance(configuration, PreviousSessionBodyPctConfiguration):
            raise TypeError(
                "PreviousSessionBodyPctStrategy requires a PreviousSessionBodyPctConfiguration"
            )
        previous = self._previous_session(context)
        if previous is None:
            return StrategyEvaluation(
                instrument=context.instrument,
                context_version=context.version,
                status=EvaluationStatus.SKIPPED,
                reason_codes=(_REASON_NO_PREVIOUS,),
            )
        candle = previous.candle
        result = compute_previous_session_body(candle.open_price, candle.close_price)
        return StrategyEvaluation(
            instrument=context.instrument,
            context_version=context.version,
            status=EvaluationStatus.MATCHED,
            score=None,
            reason_codes=(_REASON_VALID,),
            metrics=_metrics(result, previous),
        )

    @staticmethod
    def _previous_session(context: MarketContext) -> PreviousSessionFacts | None:
        """Return the authoritative previous-session facts, or ``None`` when absent."""
        if context.historical is None:
            return None
        return context.historical.previous_session


def _metrics(
    result: PreviousSessionBodyResult, previous: PreviousSessionFacts
) -> tuple[MetricEntry, ...]:
    """Build the structured body metrics (Decimal geometry + authoritative source date)."""
    candle = previous.candle
    return (
        MetricEntry(name="previous_body_pct", value=result.previous_body_pct),
        MetricEntry(name="previous_body", value=result.previous_body),
        MetricEntry(name="previous_open", value=candle.open_price),
        MetricEntry(name="previous_close", value=candle.close_price),
        MetricEntry(name="source_session_date", value=previous.trading_date.isoformat()),
    )
