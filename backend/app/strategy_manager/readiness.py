"""Pure readiness gating of a strategy against a MarketContext (P5.3; docs/07 §16).

Verifies a strategy's declared requirements, context-version compatibility, and
supplied configuration against the *current* MarketContext only — it never fetches,
warms, or fabricates facts (P5.4 owns requirement/warmup activation). Historical
series are authoritative by construction; live-timeframe requirements under
``AUTHORITATIVE_ONLY`` demand at least one finalized candle (ADR-006 — a partial or
incomplete candle is never authoritative).
"""

from __future__ import annotations

from datetime import timedelta

from app.market_engine.context import MarketContext, SessionStatisticsQuality
from app.strategies.configuration import StrategyConfiguration
from app.strategies.descriptor import StrategyDescriptor
from app.strategies.enums import CandleCompleteness, FactNeed
from app.strategies.requirements import StrategyRequirements
from app.strategy_manager.records import Readiness

_ZERO_AGE = timedelta(0)


def assess_readiness(
    *,
    descriptor: StrategyDescriptor,
    requirements: StrategyRequirements,
    configuration: StrategyConfiguration | None,
    configuration_type: type[StrategyConfiguration],
    context: MarketContext,
) -> Readiness:
    """Return ``READY`` or the first unmet gate for a strategy against a context."""
    if not _version_compatible(descriptor, requirements, context):
        return Readiness.INCOMPATIBLE_CONTEXT
    if configuration is None or not isinstance(configuration, configuration_type):
        return Readiness.MISSING_CONFIGURATION
    if not _facts_ready(requirements.fact_needs, context):
        return Readiness.MISSING_FACTS
    if not _historical_ready(requirements, context):
        return Readiness.MISSING_HISTORICAL
    if not _live_ready(requirements, context):
        return Readiness.MISSING_LIVE_TIMEFRAME
    session_statistics_verdict = _session_statistics_verdict(requirements, context)
    if session_statistics_verdict is not None:
        return session_statistics_verdict
    return Readiness.READY


def _version_compatible(
    descriptor: StrategyDescriptor, requirements: StrategyRequirements, context: MarketContext
) -> bool:
    minimum = max(descriptor.min_context_version, requirements.min_context_version)
    if context.version < minimum:
        return False
    return (
        descriptor.max_context_version is None or context.version <= descriptor.max_context_version
    )


def _facts_ready(fact_needs: tuple[FactNeed, ...], context: MarketContext) -> bool:
    # SESSION_STATISTICS has its own authority + freshness gate (below), not a presence check.
    return all(
        _fact_available(need, context)
        for need in fact_needs
        if need is not FactNeed.SESSION_STATISTICS
    )


def _fact_available(need: FactNeed, context: MarketContext) -> bool:
    if need is FactNeed.LATEST_TICK:
        return context.latest_tick is not None
    if need is FactNeed.LATEST_QUOTE:
        return context.latest_quote is not None
    if need is FactNeed.SESSION:
        return context.session is not None
    return context.historical is not None and context.historical.previous_session is not None


def _historical_ready(requirements: StrategyRequirements, context: MarketContext) -> bool:
    if not requirements.historical:
        return True
    if context.historical is None:
        return False
    by_timeframe = {series.timeframe: series for series in context.historical.series}
    return all(
        req.timeframe in by_timeframe and len(by_timeframe[req.timeframe].candles) >= req.lookback
        for req in requirements.historical
    )


def _session_statistics_verdict(
    requirements: StrategyRequirements, context: MarketContext
) -> Readiness | None:
    """Gate a SESSION_STATISTICS consumer on presence, authority, and freshness (ADR-009).

    Returns the first failing verdict, or ``None`` when the fact is not required or is
    satisfied. Reads only the MarketContext (never InstrumentState): authority alone is
    insufficient — an authoritative snapshot older than the declared ``max_age`` (or one
    the consumer cannot prove fresh) fails closed. Freshness is measured as
    ``context.observed_at - session_statistics.as_of``.
    """
    if FactNeed.SESSION_STATISTICS not in requirements.fact_needs:
        return None
    session = context.session
    statistics = context.session_statistics
    if session is None or statistics is None or statistics.trading_date != session.trading_date:
        return Readiness.MISSING_SESSION_STATISTICS
    if statistics.quality is not SessionStatisticsQuality.AUTHORITATIVE:
        return Readiness.SESSION_STATISTICS_NOT_AUTHORITATIVE
    max_age = _freshness_for(requirements, FactNeed.SESSION_STATISTICS)
    if max_age is None:
        return Readiness.SESSION_STATISTICS_STALE  # no declared bound — cannot prove freshness
    age = context.observed_at - statistics.as_of
    if age < _ZERO_AGE or age > max_age:
        return Readiness.SESSION_STATISTICS_STALE  # negative (anomaly) or older than allowed
    return None


def _freshness_for(requirements: StrategyRequirements, fact: FactNeed) -> timedelta | None:
    for entry in requirements.freshness:
        if entry.fact is fact:
            return entry.max_age
    return None


def _live_ready(requirements: StrategyRequirements, context: MarketContext) -> bool:
    if not requirements.live_timeframes:
        return True
    by_timeframe = {candles.timeframe: candles for candles in context.candle_sets}
    authoritative = requirements.candle_completeness is CandleCompleteness.AUTHORITATIVE_ONLY
    for timeframe in requirements.live_timeframes:
        candles = by_timeframe.get(timeframe)
        if candles is None:
            return False
        if authoritative and not candles.finalized:
            return False
    return True
