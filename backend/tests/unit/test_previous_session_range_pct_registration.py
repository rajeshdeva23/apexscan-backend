"""Previous Session Range % catalog registration tests (ADR-013 REG; PSR22/§27-29)."""

from __future__ import annotations

from app.services.cross_instrument_scanner import ScannerOrdering
from app.services.strategy_catalog import production_catalog
from app.strategies.enums import FactNeed, StrategyTrigger

_STRATEGY = "previous_session_range_pct"


def test_production_catalog_registers_psr_with_descending_policy() -> None:
    entry = production_catalog().resolve((_STRATEGY,))[0]
    assert entry.strategy_id == _STRATEGY
    assert entry.ranking_policy is not None
    assert entry.ranking_policy.strategy_id == _STRATEGY
    assert entry.ranking_policy.metric_name == "previous_range_pct"
    assert entry.ranking_policy.ordering is ScannerOrdering.DESCENDING


def test_disabled_strategy_contributes_no_entry() -> None:
    # Resolving only Narrow CPR must not pull in PSR; empty enablement yields nothing.
    resolved = production_catalog().resolve(("narrow_cpr",))
    assert [e.strategy_id for e in resolved] == ["narrow_cpr"]
    assert production_catalog().resolve(()) == ()


def test_enabled_strategy_declares_only_one_session_lookback() -> None:
    reqs = production_catalog().resolve((_STRATEGY,))[0].strategy.requirements
    assert len(reqs.historical) == 1
    assert reqs.historical[0].timeframe.is_session
    assert reqs.historical[0].lookback == 1
    assert FactNeed.PREVIOUS_SESSION in reqs.fact_needs
    assert FactNeed.SESSION_STATISTICS not in reqs.fact_needs
    assert reqs.trigger is StrategyTrigger.ON_HISTORICAL_READY
    assert reqs.live_timeframes == ()
