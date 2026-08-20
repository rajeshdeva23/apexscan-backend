"""Tests for the Strategy Engine / Strategy Manager import-boundary guard (P5.0; ADR-007)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.architecture.import_boundary import scan_market_engine
from tests.architecture.strategy_import_boundary import (
    cross_implementation_violations,
    manager_violations,
    scan_strategies,
    scan_strategy_manager,
    strategy_violations,
)

_APP_ROOT = Path(__file__).parents[2] / "app"
_STRATEGY_PKG = "app.strategies.implementations.open_high"
_MANAGER_PKG = "app.strategy_manager.manager"


# --------------------------------------------------------------------------- #
# Real-package scans — placeholders must be clean now (§22)
# --------------------------------------------------------------------------- #
def test_current_strategies_package_is_clean() -> None:
    assert scan_strategies(_APP_ROOT) == {}


def test_current_strategy_manager_package_is_clean() -> None:
    assert scan_strategy_manager(_APP_ROOT) == {}


def test_phase4_market_engine_guard_remains_green() -> None:
    assert scan_market_engine(_APP_ROOT) == {}


# --------------------------------------------------------------------------- #
# Strategy layer — forbidden imports (§3, §6, §7, §8, §9, §16, §17, §18)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import app.adapters.dhan", id="provider-package"),
        pytest.param("from app.adapters.dhan.adapter import DhanRestAdapter", id="provider-symbol"),
        pytest.param("from app.adapters.base import HistoricalDataAdapter", id="adapter-contract"),
        pytest.param("import httpx", id="http-sdk"),
        pytest.param("import websockets", id="ws-transport"),
        pytest.param("import dhanhq", id="broker-sdk"),
        pytest.param("import sqlalchemy", id="orm"),
        pytest.param("import redis", id="redis-client"),
        pytest.param("from app.database.session import get_session", id="database"),
        pytest.param("from app.repositories.base import Repository", id="repository"),
        pytest.param("from app.cache import get_redis", id="cache"),
        pytest.param("from app.events.bus import EventBus", id="event-bus-manager-only"),
        pytest.param("from app.strategy_manager.ranking import rank", id="manager-ranking"),
        pytest.param("from app.strategy_manager.state import StateStore", id="manager-state"),
        pytest.param("from app.api.v1.router import api_router", id="api"),
        pytest.param("from app.websocket import broadcast", id="websocket-layer"),
        pytest.param(
            "from app.market_engine.candle_engine import CandleEngine", id="mutation-candle"
        ),
        pytest.param("from app.market_engine.tick_engine import TickEngine", id="mutation-tick"),
        pytest.param(
            "from app.market_engine.state import InstrumentStateRegistry", id="mutation-state"
        ),
        pytest.param("from app.market_engine import CandleEngine", id="engine-root-kitchen-sink"),
        pytest.param(
            "from app.market_engine.historical.service import HistoricalWarmupService", id="warmup"
        ),
    ],
)
def test_strategy_forbidden_imports_are_flagged(source: str) -> None:
    assert strategy_violations(source, package=_STRATEGY_PKG)


# --------------------------------------------------------------------------- #
# Strategy layer — allowed read-only contracts (§21)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "source",
    [
        pytest.param("from app.market_engine.context import MarketContext", id="market-context"),
        pytest.param(
            "from app.market_engine.context import MarketState, SessionContext", id="session-facts"
        ),
        pytest.param("from app.market_engine.timeframe import Timeframe", id="timeframe"),
        pytest.param(
            "from app.market_engine.historical.context import HistoricalContext",
            id="historical-context",
        ),
        pytest.param(
            "from app.market_engine.historical.requirements import HistoricalRequirement",
            id="historical-requirement",
        ),
        pytest.param(
            "from app.schemas.market_data import Candle, Instrument", id="canonical-schemas"
        ),
        pytest.param("from app.strategies.base import Strategy", id="own-shared-contracts"),
        pytest.param("from decimal import Decimal\nfrom datetime import UTC", id="stdlib"),
        pytest.param("from pydantic import BaseModel", id="pydantic"),
    ],
)
def test_strategy_allowed_imports_pass(source: str) -> None:
    assert strategy_violations(source, package=_STRATEGY_PKG) == []


# --------------------------------------------------------------------------- #
# Cross-strategy isolation (§5)
# --------------------------------------------------------------------------- #
def test_strategy_cannot_import_sibling_implementation() -> None:
    source = "from app.strategies.implementations.narrow_cpr import NarrowCpr"
    assert cross_implementation_violations(source, package=_STRATEGY_PKG) == [
        "app.strategies.implementations.narrow_cpr"
    ]


def test_strategy_may_import_within_own_implementation_subtree() -> None:
    source = "from app.strategies.implementations.open_high.helpers import metric"
    assert cross_implementation_violations(source, package=_STRATEGY_PKG) == []


# --------------------------------------------------------------------------- #
# Strategy Manager — policy (§4, §10)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import app.adapters.dhan", id="mgr-provider"),
        pytest.param("import httpx", id="mgr-http"),
        pytest.param(
            "from app.strategies.implementations.open_high import OpenHigh", id="mgr-concrete-impl"
        ),
        pytest.param(
            "from app.strategies.implementations import narrow_cpr", id="mgr-concrete-impl-pkg"
        ),
        pytest.param(
            "from app.market_engine.candle_engine import CandleEngine", id="mgr-mutation-engine"
        ),
        pytest.param("from app.api.v1.router import api_router", id="mgr-api"),
        pytest.param("import sqlalchemy", id="mgr-orm"),
    ],
)
def test_manager_forbidden_imports_are_flagged(source: str) -> None:
    assert manager_violations(source, package=_MANAGER_PKG)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("from app.strategies.base import Strategy", id="strategy-contract"),
        pytest.param("from app.events.bus import EventBus", id="event-bus"),
        pytest.param(
            "from app.market_engine.events import MarketContextUpdated", id="context-events"
        ),
        pytest.param("from app.market_engine.context import MarketContext", id="market-context"),
        pytest.param(
            "from app.market_engine.historical.requirements import HistoricalRequirementRegistry",
            id="requirement-registry-seam",
        ),
        pytest.param("from app.core.config import Settings", id="core-config"),
        pytest.param("from app.schemas.market_data import Instrument", id="schemas"),
    ],
)
def test_manager_allowed_imports_pass(source: str) -> None:
    assert manager_violations(source, package=_MANAGER_PKG) == []


# --------------------------------------------------------------------------- #
# Resolver robustness (§26.14-16)
# --------------------------------------------------------------------------- #
def test_alias_does_not_bypass_strategy_guard() -> None:
    source = "import app.adapters.dhan.adapter as broker"
    assert strategy_violations(source, package=_STRATEGY_PKG) == ["app.adapters.dhan.adapter"]


def test_relative_import_is_resolved_and_flagged() -> None:
    # From app.strategies.implementations.open_high, `from ...` climbs to app.strategies.
    source = "from ....adapters.dhan import adapter"
    assert strategy_violations(source, package=_STRATEGY_PKG)


def test_import_order_is_irrelevant() -> None:
    source = (
        "from __future__ import annotations\n"
        "from app.market_engine.context import MarketContext\n"
        "import app.database.session\n"
        "from app.schemas.market_data import Candle\n"
    )
    assert strategy_violations(source, package=_STRATEGY_PKG) == ["app.database.session"]
