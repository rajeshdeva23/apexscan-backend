"""Production strategy catalog tests (ADR-013)."""

from __future__ import annotations

import inspect

import pytest

from app.services import strategy_catalog as catalog_module
from app.services.cross_instrument_scanner import ScannerOrdering, ScannerRankingPolicy
from app.services.strategy_catalog import (
    StrategyCatalog,
    StrategyCatalogEntry,
    UnknownEnabledStrategyError,
    production_catalog,
)
from app.strategies.configuration import StrategyConfiguration
from app.strategies.implementations.narrow_cpr import NarrowCprConfiguration, NarrowCprStrategy


def _narrow_entry() -> StrategyCatalogEntry:
    return StrategyCatalogEntry(
        strategy=NarrowCprStrategy(),
        configuration=NarrowCprConfiguration(config_version="1.0.0"),
        ranking_policy=ScannerRankingPolicy(
            "narrow_cpr", "cpr_width_pct", ScannerOrdering.ASCENDING
        ),
    )


def test_valid_entry_exposes_strategy_id() -> None:
    assert _narrow_entry().strategy_id == "narrow_cpr"


def test_entry_rejects_policy_id_mismatch() -> None:
    with pytest.raises(ValueError, match="does not match"):
        StrategyCatalogEntry(
            strategy=NarrowCprStrategy(),
            configuration=NarrowCprConfiguration(config_version="1.0.0"),
            ranking_policy=ScannerRankingPolicy(
                "wrong_id", "cpr_width_pct", ScannerOrdering.ASCENDING
            ),
        )


def test_entry_rejects_wrong_configuration_type() -> None:
    with pytest.raises(ValueError, match="must be a NarrowCprConfiguration"):
        StrategyCatalogEntry(
            strategy=NarrowCprStrategy(),
            configuration=StrategyConfiguration(config_version="1.0.0"),  # base, not NarrowCpr
        )


def test_catalog_rejects_duplicate_strategy_ids() -> None:
    with pytest.raises(ValueError, match="duplicate strategy id"):
        StrategyCatalog((_narrow_entry(), _narrow_entry()))


def test_resolve_returns_entries_in_order() -> None:
    catalog = StrategyCatalog((_narrow_entry(),))
    resolved = catalog.resolve(("narrow_cpr",))
    assert len(resolved) == 1
    assert resolved[0].strategy_id == "narrow_cpr"


def test_resolve_unknown_id_fails_closed() -> None:
    catalog = StrategyCatalog((_narrow_entry(),))
    with pytest.raises(UnknownEnabledStrategyError, match="ghost"):
        catalog.resolve(("ghost",))


def test_resolve_empty_is_empty() -> None:
    assert production_catalog().resolve(()) == ()


def test_production_catalog_contains_narrow_cpr() -> None:
    entry = production_catalog().resolve(("narrow_cpr",))[0]
    assert entry.ranking_policy is not None
    assert entry.ranking_policy.metric_name == "cpr_width_pct"
    assert entry.ranking_policy.ordering is ScannerOrdering.ASCENDING
    assert isinstance(entry.configuration, NarrowCprConfiguration)


def test_catalog_is_provider_neutral() -> None:
    source = inspect.getsource(catalog_module).lower()
    for forbidden in ("dhan", "httpx", "websocket", "sqlalchemy", "redis", "security_id"):
        assert forbidden not in source
