"""Previous Session Relative Range catalog registration tests (ADR-013 REG; PSRR §23)."""

from __future__ import annotations

from app.market_engine.historical.requirements import (
    HistoricalRequirement,
    HistoricalRequirementRegistry,
)
from app.market_engine.timeframe import Timeframe
from app.services.cross_instrument_scanner import ScannerOrdering
from app.services.strategy_catalog import production_catalog
from app.strategies.enums import FactNeed, StrategyTrigger

_STRATEGY = "previous_session_relative_range"
_SESSION = Timeframe.session()


def test_production_catalog_registers_psrr_with_ascending_policy() -> None:
    entry = production_catalog().resolve((_STRATEGY,))[0]
    assert entry.strategy_id == _STRATEGY
    assert entry.ranking_policy is not None
    assert entry.ranking_policy.strategy_id == _STRATEGY
    assert entry.ranking_policy.metric_name == "relative_range_ratio"
    assert entry.ranking_policy.ordering is ScannerOrdering.ASCENDING


def test_enabled_strategy_declares_21_session_lookback() -> None:
    reqs = production_catalog().resolve((_STRATEGY,))[0].strategy.requirements
    assert len(reqs.historical) == 1
    assert reqs.historical[0].timeframe.is_session
    assert reqs.historical[0].lookback == 21
    assert FactNeed.PREVIOUS_SESSION in reqs.fact_needs
    assert FactNeed.SESSION_STATISTICS not in reqs.fact_needs
    assert reqs.trigger is StrategyTrigger.ON_HISTORICAL_READY


def test_disabled_strategy_contributes_no_entry() -> None:
    catalog = production_catalog()
    assert catalog.resolve(()) == ()
    for other in ("narrow_cpr", "previous_session_range_pct", "previous_session_body_pct"):
        assert all(e.strategy_id != _STRATEGY for e in catalog.resolve((other,)))


def test_requirement_union_is_max_lookback_21_when_all_enabled() -> None:
    registry = HistoricalRequirementRegistry()
    # three frozen strategies at lookback 1, PSRR at 21
    for consumer, lookback in (("cpr", 1), ("range", 1), ("body", 1), ("relative", 21)):
        registry.register(
            consumer, frozenset({HistoricalRequirement(timeframe=_SESSION, lookback=lookback)})
        )
    session = [r for r in registry.effective_requirements() if r.timeframe.is_session]
    assert len(session) == 1
    assert session[0].lookback == 21  # max(1,1,1,21), not sum
