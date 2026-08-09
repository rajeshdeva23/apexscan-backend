"""Tests for the Market Engine import-boundary guard (docs/03 §3.6, ADR-003)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.architecture.import_boundary import (
    forbidden_imports,
    imported_modules,
    scan_market_engine,
)

_APP_ROOT = Path(__file__).parents[2] / "app"
_ENGINE_PACKAGE = "app.market_engine"

# The historical layer (P4.5) must be even stricter than the engine baseline: it
# performs no provider I/O in P4.5A, so it must import neither a concrete adapter
# nor the adapter contract package, nor durable persistence, API, or strategies.
_HISTORICAL_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "app.adapters",
    "app.database",
    "app.repositories",
    "app.api",
    "app.strategies",
    "app.strategy_manager",
)


def test_market_engine_currently_has_no_boundary_violations() -> None:
    """The real Market Engine package must never import a forbidden dependency."""
    assert scan_market_engine(_APP_ROOT) == {}


def test_historical_layer_imports_no_provider_persistence_or_strategy() -> None:
    """P4.5A historical modules must not import providers, persistence, or strategies."""
    historical_dir = _APP_ROOT / "market_engine" / "historical"
    offenders: dict[str, list[str]] = {}
    for path in sorted(historical_dir.rglob("*.py")):
        relative = path.relative_to(_APP_ROOT.parent).with_suffix("")
        package = ".".join(relative.parts[:-1])
        bad = [
            module
            for module in imported_modules(path.read_text(encoding="utf-8"), package=package)
            if any(
                module == prefix or module.startswith(f"{prefix}.")
                for prefix in _HISTORICAL_FORBIDDEN_PREFIXES
            )
        ]
        if bad:
            offenders[str(path)] = bad
    assert offenders == {}


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import app.adapters.dhan", id="dhan-package"),
        pytest.param(
            "from app.adapters.dhan.live import decode_standard_live_packet",
            id="dhan-live-symbol",
        ),
        pytest.param("from app.adapters.dhan import DhanRestAdapter", id="dhan-adapter"),
        pytest.param("import app.adapters.binance", id="other-concrete-broker"),
        pytest.param("import websockets", id="websocket-transport"),
        pytest.param("import pyotp", id="broker-auth-sdk"),
        pytest.param("from app.strategies import base", id="strategy-module"),
        pytest.param("from app.strategy_manager import Manager", id="strategy-manager"),
        pytest.param("import app.api.deps", id="api-layer"),
        pytest.param("from app.repositories.base import Repository", id="durable-persistence"),
        pytest.param("from app.database.session import get_session", id="database-session"),
    ],
)
def test_forbidden_imports_are_detected(source: str) -> None:
    """A forbidden import in Market Engine code must be flagged by the guard."""
    assert forbidden_imports(source, package=_ENGINE_PACKAGE)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("from app.schemas.market_data import Tick", id="canonical-contracts"),
        pytest.param(
            "from app.adapters.base.broker_adapter import BrokerAdapter",
            id="adapter-contract",
        ),
        pytest.param("from app.core.config import Settings", id="core-config"),
        pytest.param("from app.cache.redis_client import redis_lifecycle", id="cache"),
        pytest.param("from app.events.bus import EventBus", id="events-seam"),
        pytest.param("from app.market_engine.context import MarketContext", id="engine-self"),
        pytest.param("import asyncio\nfrom datetime import UTC", id="stdlib"),
        pytest.param("from pydantic import BaseModel", id="pydantic"),
    ],
)
def test_allowed_imports_pass(source: str) -> None:
    """Permitted canonical/contract/core/event imports must not be flagged."""
    assert forbidden_imports(source, package=_ENGINE_PACKAGE) == []


def test_guard_is_insensitive_to_import_order_and_aliasing() -> None:
    """AST analysis catches a forbidden import regardless of ordering or alias."""
    source = (
        "from __future__ import annotations\n"
        "import asyncio\n"
        "from app.schemas.market_data import Tick\n"
        "import app.adapters.dhan.adapter as broker\n"
    )
    assert forbidden_imports(source, package=_ENGINE_PACKAGE) == ["app.adapters.dhan.adapter"]
