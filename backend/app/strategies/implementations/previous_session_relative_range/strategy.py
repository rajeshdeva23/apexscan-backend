"""Previous Session Relative Range strategy — V1 (ADR-007 PSRR strategy specification).

The first multi-session scanner: ranks instruments by how compressed the previous
completed session (D-1) was relative to a fixed 20-session normalized-range baseline
(PSRR1). It declares ``HistoricalRequirement(Timeframe.session(), lookback=21)`` +
``FactNeed.PREVIOUS_SESSION`` (PSRR6/PSRR12), reads the 21-candle session series from
``context.historical.series`` (oldest->newest), takes ``series[-1]`` as D-1 and
``series[:-1]`` as the 20-session baseline (D-1 excluded from its own baseline; PSRR7),
and computes ``relative_range_ratio`` via the pure calculator.

Invariants: pure/deterministic; provider-neutral (canonical ``MarketContext`` only; PSRR24);
non-repainting/no-look-ahead (completed history only; PSRR22/PSRR23); non-directional;
basis-safe (per-session dimensionless ratios; PSRR21). ``score`` is ``None`` (PSRR16); the
scanner ranks **ascending** by ``relative_range_ratio`` — smallest (most compressed vs own
baseline) first (PSRR17).
"""

from __future__ import annotations

from app.market_engine.context import MarketContext
from app.market_engine.historical.context import HistoricalSeries, PreviousSessionFacts
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
from app.strategies.implementations.previous_session_relative_range.calculator import (
    DegenerateBaselineError,
    PreviousSessionRelativeRangeResult,
    compute_previous_session_relative_range,
)
from app.strategies.implementations.previous_session_relative_range.configuration import (
    PreviousSessionRelativeRangeConfiguration,
)
from app.strategies.requirements import StrategyRequirements
from app.strategies.results import MetricEntry, StrategyEvaluation

_STRATEGY_ID = "previous_session_relative_range"
_VERSION = "1.0.0"
BASELINE_SESSIONS = 20
REQUIRED_SESSIONS = 21  # 20 baseline + the D-1 subject

# Reason codes (structured explainability; docs/07 §12 — never English text).
_REASON_VALID = "PREVIOUS_SESSION_RELATIVE_RANGE_VALID"
_REASON_NO_HISTORY = "PREVIOUS_SESSION_RELATIVE_RANGE_NO_HISTORY"
_REASON_DEGENERATE = "PREVIOUS_SESSION_RELATIVE_RANGE_DEGENERATE_BASELINE"

_DESCRIPTOR = StrategyDescriptor(
    strategy_id=_STRATEGY_ID,
    display_name="Previous Session Relative Range",
    description=(
        "Ranks instruments by how compressed the previous completed session's normalized "
        "range was relative to the instrument's own 20-session median. A non-directional "
        "historical compression feature — never a buy/sell signal."
    ),
    version=_VERSION,
    category=StrategyCategory.MARKET_STRUCTURE,
    emission_policy=EmissionPolicy.ONE_SHOT_PER_SESSION,
)

_REQUIREMENTS = StrategyRequirements(
    historical=(HistoricalRequirement(timeframe=Timeframe.session(), lookback=REQUIRED_SESSIONS),),
    fact_needs=(FactNeed.PREVIOUS_SESSION,),
    trigger=StrategyTrigger.ON_HISTORICAL_READY,
    candle_completeness=CandleCompleteness.AUTHORITATIVE_ONLY,
)


class PreviousSessionRelativeRangeStrategy:
    """The V1 Previous Session Relative Range strategy (conforms to the ``Strategy`` protocol)."""

    @property
    def descriptor(self) -> StrategyDescriptor:
        """Return the immutable identity/metadata for this strategy."""
        return _DESCRIPTOR

    @property
    def requirements(self) -> StrategyRequirements:
        """Return the declared requirements: 21 session lookback, previous-session fact."""
        return _REQUIREMENTS

    @property
    def configuration_type(self) -> type[StrategyConfiguration]:
        """Return the concrete configuration type this strategy validates against."""
        return PreviousSessionRelativeRangeConfiguration

    def evaluate(
        self,
        context: MarketContext,
        configuration: StrategyConfiguration,
        metadata: StrategyEvaluationMetadata,
    ) -> StrategyEvaluation:
        """Evaluate the previous session's compression relative to its 20-session baseline.

        Reads only the completed 21-session series (no current-day field). Requires exactly
        ``REQUIRED_SESSIONS`` candles and the authoritative previous-session fact; otherwise
        ``SKIPPED`` (fail-closed) — normally unreachable, since readiness blocks evaluation
        until the full session requirement is satisfied. A zero baseline median yields
        ``SKIPPED`` (degenerate); a zero subject range with a positive baseline is a valid
        ratio of 0 (PSRR10).
        """
        if not isinstance(configuration, PreviousSessionRelativeRangeConfiguration):
            raise TypeError(
                "PreviousSessionRelativeRangeStrategy requires a "
                "PreviousSessionRelativeRangeConfiguration"
            )
        series = self._session_series(context)
        previous = context.historical.previous_session if context.historical is not None else None
        if series is None or len(series.candles) != REQUIRED_SESSIONS or previous is None:
            return StrategyEvaluation(
                instrument=context.instrument,
                context_version=context.version,
                status=EvaluationStatus.SKIPPED,
                reason_codes=(_REASON_NO_HISTORY,),
            )
        subject = series.candles[-1]
        baseline = series.candles[:-1]
        try:
            result = compute_previous_session_relative_range(
                (subject.open_price, subject.high_price, subject.low_price),
                [(c.open_price, c.high_price, c.low_price) for c in baseline],
            )
        except DegenerateBaselineError:
            return StrategyEvaluation(
                instrument=context.instrument,
                context_version=context.version,
                status=EvaluationStatus.SKIPPED,
                reason_codes=(_REASON_DEGENERATE,),
            )
        return StrategyEvaluation(
            instrument=context.instrument,
            context_version=context.version,
            status=EvaluationStatus.MATCHED,
            score=None,
            reason_codes=(_REASON_VALID,),
            metrics=_metrics(result, previous),
        )

    @staticmethod
    def _session_series(context: MarketContext) -> HistoricalSeries | None:
        """Return the session-timeframe historical series, or ``None`` when absent."""
        if context.historical is None:
            return None
        for series in context.historical.series:
            if series.timeframe.is_session:
                return series
        return None


def _metrics(
    result: PreviousSessionRelativeRangeResult, previous: PreviousSessionFacts
) -> tuple[MetricEntry, ...]:
    """Build the structured relative-range metrics (Decimal geometry + authoritative D-1 date)."""
    return (
        MetricEntry(name="relative_range_ratio", value=result.relative_range_ratio),
        MetricEntry(name="previous_range_pct", value=result.previous_range_pct),
        MetricEntry(name="baseline_range_pct", value=result.baseline_range_pct),
        MetricEntry(name="baseline_sessions", value=BASELINE_SESSIONS),
        MetricEntry(name="source_session_date", value=previous.trading_date.isoformat()),
    )
