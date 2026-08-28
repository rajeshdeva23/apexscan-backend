"""Open=High / Open=Low current-session strategies — V1 (ADR-009 CSOA22).

Two directional opening-session scanner features over the **authoritative
current-session** open/high/low (ADR-008/009). Open=High matches when the
session traded no higher than its open (open == session high) — a bearish
opening structure; Open=Low matches when it traded no lower than its open
(open == session low) — a bullish one. They are scanner classifications only,
never trade signals (no order/BUY/SELL).

Both declare ``FactNeed.SESSION_STATISTICS`` with a freshness bound, so the
Strategy Manager readiness gate (``strategy_manager.readiness``) admits them
only when ``MarketContext.session_statistics`` is ``AUTHORITATIVE`` and fresh —
the single authority-enforcement point. The strategies additionally fail closed
inside ``evaluate`` (defence in depth): absent/unauthoritative statistics yield
``SKIPPED``, never a fabricated match, and they never read a tick price, candle
extremum, previous-session value, or process-start extremum as a substitute.

Invariants: pure/deterministic (no clock/network/random/global mutation);
provider-neutral (reads only the broker-neutral ``MarketContext``); exact
``Decimal`` equality (no tolerance — provider prices are already canonical
Decimals and ``SessionStatistics`` enforces ``low <= open <= high``). The scanner
ranks matches by ``session_range_pct`` descending (widest open-to-extreme travel
first); ``score`` is left ``None`` (no governed absolute scale).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.market_engine.context import MarketContext, SessionStatistics, SessionStatisticsQuality
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
from app.strategies.implementations.open_extreme.configuration import OpenExtremeConfiguration
from app.strategies.requirements import FactFreshnessRequirement, StrategyRequirements
from app.strategies.results import MetricEntry, StrategyEvaluation

_VERSION = "1.0.0"
_RANKING_METRIC = "session_range_pct"
# Current-session OHLC must be no older than this to evaluate; aligned with the live-feed
# hard-stale threshold (a feed silent longer than this is treated as stuck, DEPLOY-9.6).
_SESSION_STATISTICS_MAX_AGE = timedelta(minutes=2)

# Structured reason codes (docs/07 §12 — codes, never English text).
_REASON_UNAVAILABLE = "SESSION_STATISTICS_UNAVAILABLE"
_REASON_OPEN_EQUALS_HIGH = "OPEN_EQUALS_HIGH"
_REASON_HIGH_ABOVE_OPEN = "HIGH_ABOVE_OPEN"
_REASON_OPEN_EQUALS_LOW = "OPEN_EQUALS_LOW"
_REASON_LOW_BELOW_OPEN = "LOW_BELOW_OPEN"

_REQUIREMENTS = StrategyRequirements(
    fact_needs=(FactNeed.SESSION, FactNeed.SESSION_STATISTICS),
    trigger=StrategyTrigger.ON_TICK,
    candle_completeness=CandleCompleteness.AUTHORITATIVE_ONLY,
    freshness=(
        FactFreshnessRequirement(
            fact=FactNeed.SESSION_STATISTICS, max_age=_SESSION_STATISTICS_MAX_AGE
        ),
    ),
)


def _authoritative_statistics(context: MarketContext) -> SessionStatistics | None:
    """Return the current-session statistics only when authoritative, else ``None``."""
    statistics = context.session_statistics
    if statistics is None or statistics.quality is not SessionStatisticsQuality.AUTHORITATIVE:
        return None
    return statistics


def _session_metrics(statistics: SessionStatistics) -> tuple[MetricEntry, ...]:
    """Build explainability metrics incl. the ``session_range_pct`` ranking metric."""
    open_price = statistics.open_price
    high_price = statistics.high_price
    low_price = statistics.low_price
    assert open_price is not None and high_price is not None and low_price is not None
    range_pct = (high_price - low_price) / open_price * Decimal(100)
    return (
        MetricEntry(name=_RANKING_METRIC, value=range_pct),
        MetricEntry(name="session_open", value=open_price),
        MetricEntry(name="session_high", value=high_price),
        MetricEntry(name="session_low", value=low_price),
    )


def _evaluate_open_extreme(
    context: MarketContext,
    *,
    match_when_equals_high: bool,
    matched_reason: str,
    no_match_reason: str,
) -> StrategyEvaluation:
    """Shared pure evaluation: exact open==extreme match over authoritative statistics."""
    statistics = _authoritative_statistics(context)
    if statistics is None:
        return StrategyEvaluation(
            instrument=context.instrument,
            context_version=context.version,
            status=EvaluationStatus.SKIPPED,
            reason_codes=(_REASON_UNAVAILABLE,),
        )
    extreme = statistics.high_price if match_when_equals_high else statistics.low_price
    matched = statistics.open_price == extreme
    return StrategyEvaluation(
        instrument=context.instrument,
        context_version=context.version,
        status=EvaluationStatus.MATCHED if matched else EvaluationStatus.NO_MATCH,
        score=None,
        reason_codes=((matched_reason,) if matched else (no_match_reason,)),
        metrics=_session_metrics(statistics),
    )


class OpenHighStrategy:
    """Open=High: authoritative session open == session high (bearish opening feature)."""

    _DESCRIPTOR = StrategyDescriptor(
        strategy_id="open_high",
        display_name="Open = High",
        description=(
            "Flags instruments whose authoritative current-session open equals the "
            "session high (the session has not traded above its open) — a bearish "
            "opening-structure scanner feature, never a buy/sell signal."
        ),
        version=_VERSION,
        category=StrategyCategory.OPENING_SESSION,
        emission_policy=EmissionPolicy.EDGE_TRIGGERED,
    )

    @property
    def descriptor(self) -> StrategyDescriptor:
        """Return the immutable identity/metadata for this strategy."""
        return self._DESCRIPTOR

    @property
    def requirements(self) -> StrategyRequirements:
        """Return the declared requirements: authoritative, fresh session statistics."""
        return _REQUIREMENTS

    @property
    def configuration_type(self) -> type[StrategyConfiguration]:
        """Return the concrete configuration type this strategy validates against."""
        return OpenExtremeConfiguration

    def evaluate(
        self,
        context: MarketContext,
        configuration: StrategyConfiguration,
        metadata: StrategyEvaluationMetadata,
    ) -> StrategyEvaluation:
        """Match when the authoritative session open equals the session high (fail-closed)."""
        return _evaluate_open_extreme(
            context,
            match_when_equals_high=True,
            matched_reason=_REASON_OPEN_EQUALS_HIGH,
            no_match_reason=_REASON_HIGH_ABOVE_OPEN,
        )


class OpenLowStrategy:
    """Open=Low: authoritative session open == session low (bullish opening feature)."""

    _DESCRIPTOR = StrategyDescriptor(
        strategy_id="open_low",
        display_name="Open = Low",
        description=(
            "Flags instruments whose authoritative current-session open equals the "
            "session low (the session has not traded below its open) — a bullish "
            "opening-structure scanner feature, never a buy/sell signal."
        ),
        version=_VERSION,
        category=StrategyCategory.OPENING_SESSION,
        emission_policy=EmissionPolicy.EDGE_TRIGGERED,
    )

    @property
    def descriptor(self) -> StrategyDescriptor:
        """Return the immutable identity/metadata for this strategy."""
        return self._DESCRIPTOR

    @property
    def requirements(self) -> StrategyRequirements:
        """Return the declared requirements: authoritative, fresh session statistics."""
        return _REQUIREMENTS

    @property
    def configuration_type(self) -> type[StrategyConfiguration]:
        """Return the concrete configuration type this strategy validates against."""
        return OpenExtremeConfiguration

    def evaluate(
        self,
        context: MarketContext,
        configuration: StrategyConfiguration,
        metadata: StrategyEvaluationMetadata,
    ) -> StrategyEvaluation:
        """Match when the authoritative session open equals the session low (fail-closed)."""
        return _evaluate_open_extreme(
            context,
            match_when_equals_high=False,
            matched_reason=_REASON_OPEN_EQUALS_LOW,
            no_match_reason=_REASON_LOW_BELOW_OPEN,
        )
