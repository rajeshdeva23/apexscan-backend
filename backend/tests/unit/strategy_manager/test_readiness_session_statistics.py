"""Readiness gating for SESSION_STATISTICS: authority + freshness (P4.6E5; ADR-009 D6)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.market_engine.context import (
    MarketContext,
    MarketState,
    SessionContext,
    SessionStatistics,
    SessionStatisticsQuality,
)
from app.schemas.market_data import Instrument
from app.strategies.configuration import StrategyConfiguration
from app.strategies.descriptor import StrategyDescriptor
from app.strategies.enums import (
    CandleCompleteness,
    EmissionPolicy,
    FactNeed,
    StrategyCategory,
    StrategyTrigger,
)
from app.strategies.requirements import FactFreshnessRequirement, StrategyRequirements
from app.strategy_manager.readiness import assess_readiness
from app.strategy_manager.records import Readiness

_INSTRUMENT = Instrument(exchange="NSE", symbol="RELIANCE")
_DATE = date(2026, 8, 6)
_AS_OF = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)
_MAX_AGE = timedelta(seconds=5)


def _descriptor() -> StrategyDescriptor:
    return StrategyDescriptor(
        strategy_id="alpha",
        display_name="Alpha",
        description="A test strategy.",
        version="1.0.0",
        category=StrategyCategory.OPENING_SESSION,
        emission_policy=EmissionPolicy.EDGE_TRIGGERED,
    )


def _requirements(*, with_freshness: bool = True) -> StrategyRequirements:
    freshness = (
        (FactFreshnessRequirement(fact=FactNeed.SESSION_STATISTICS, max_age=_MAX_AGE),)
        if with_freshness
        else ()
    )
    return StrategyRequirements(
        trigger=StrategyTrigger.ON_TICK,
        candle_completeness=CandleCompleteness.PARTIAL_ALLOWED,
        fact_needs=(FactNeed.SESSION_STATISTICS,),
        freshness=freshness,
    )


def _statistics(
    *,
    quality: SessionStatisticsQuality = SessionStatisticsQuality.AUTHORITATIVE,
    trading_date: date = _DATE,
    as_of: datetime = _AS_OF,
) -> SessionStatistics:
    priced = quality is SessionStatisticsQuality.AUTHORITATIVE
    return SessionStatistics(
        trading_date=trading_date,
        open_price=Decimal("100") if priced else None,
        high_price=Decimal("105") if priced else None,
        low_price=Decimal("98") if priced else None,
        quality=quality,
        as_of=as_of,
    )


def _context(
    *,
    observed_at: datetime = _AS_OF,
    session_trading_date: date | None = _DATE,
    statistics: SessionStatistics | None,
) -> MarketContext:
    session = (
        SessionContext(
            trading_date=session_trading_date,
            market_state=MarketState.LIVE_SESSION,
            exchange_timezone="Asia/Kolkata",
        )
        if session_trading_date is not None
        else None
    )
    return MarketContext.initial(
        _INSTRUMENT,
        sequence=1,
        event_timestamp=observed_at,
        observed_at=observed_at,
        latest_tick=None,
        session=session,
        session_statistics=statistics,
    )


def _assess(
    context: MarketContext, *, requirements: StrategyRequirements | None = None
) -> Readiness:
    return assess_readiness(
        descriptor=_descriptor(),
        requirements=requirements if requirements is not None else _requirements(),
        configuration=StrategyConfiguration(config_version="1.0.0"),
        configuration_type=StrategyConfiguration,
        context=context,
    )


def test_missing_statistics_is_not_ready() -> None:
    context = _context(statistics=None)
    assert _assess(context) is Readiness.MISSING_SESSION_STATISTICS


def test_not_authoritative_is_not_ready() -> None:
    context = _context(statistics=_statistics(quality=SessionStatisticsQuality.UNAVAILABLE))
    assert _assess(context) is Readiness.SESSION_STATISTICS_NOT_AUTHORITATIVE


def test_authoritative_and_fresh_is_ready() -> None:
    context = _context(observed_at=_AS_OF + timedelta(seconds=3), statistics=_statistics())
    assert _assess(context) is Readiness.READY


def test_authoritative_but_stale_is_not_ready() -> None:
    context = _context(observed_at=_AS_OF + timedelta(seconds=6), statistics=_statistics())
    assert _assess(context) is Readiness.SESSION_STATISTICS_STALE


def test_exact_max_age_is_fresh() -> None:
    context = _context(observed_at=_AS_OF + _MAX_AGE, statistics=_statistics())
    assert _assess(context) is Readiness.READY  # age == max_age is inclusive


def test_negative_age_fails_closed() -> None:
    context = _context(observed_at=_AS_OF - timedelta(seconds=1), statistics=_statistics())
    assert _assess(context) is Readiness.SESSION_STATISTICS_STALE


def test_wrong_trading_date_is_not_ready() -> None:
    context = _context(statistics=_statistics(trading_date=date(2026, 8, 5)))
    assert _assess(context) is Readiness.MISSING_SESSION_STATISTICS


def test_missing_session_is_not_ready() -> None:
    context = _context(session_trading_date=None, statistics=_statistics())
    # SESSION_STATISTICS gate reports missing; SESSION is not itself required here.
    assert _assess(context) is Readiness.MISSING_SESSION_STATISTICS


def test_declared_without_freshness_bound_is_not_ready() -> None:
    context = _context(statistics=_statistics())
    assert _assess(context, requirements=_requirements(with_freshness=False)) is (
        Readiness.SESSION_STATISTICS_STALE
    )


def test_strategy_not_requiring_session_statistics_is_unaffected() -> None:
    requirements = StrategyRequirements(
        trigger=StrategyTrigger.ON_TICK,
        candle_completeness=CandleCompleteness.PARTIAL_ALLOWED,
        fact_needs=(),
    )
    context = _context(statistics=None)  # no stats present, but not required
    assert _assess(context, requirements=requirements) is Readiness.READY
