"""Import-boundary + inertness proofs for the readiness package (DECOUPLING PHASE G).

The readiness engine reuses strategy requirement metadata, Phase-F history, Phase-E universe, and
the clock, but imports no Dhan adapter / IPC transport / websocket / composition code. Strategy and
strategy-manager modules do NOT import it (Phase G owns readiness; it is not wired into strategy
execution), and production composition never constructs the engine.
"""

from __future__ import annotations

from pathlib import Path

from tests.architecture.import_boundary import imported_modules

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"

_FORBIDDEN_IN_READINESS = (
    "app.adapters.dhan",
    "app.market_ipc",
    "app.services",
    "websockets",
    "pyotp",
)

# Layers that must NOT import the readiness package (it must not creep into strategy execution).
_READINESS_UNAWARE_LAYERS = (
    _APP_ROOT / "adapters",
    _APP_ROOT / "market_ipc",
    _APP_ROOT / "market_universe",
    _APP_ROOT / "market_history",
    _APP_ROOT / "market_intelligence" / "sector",
    _APP_ROOT / "strategies",
    _APP_ROOT / "strategy_manager",
    _APP_ROOT / "services",
)


def _modules(path: Path) -> list[str]:
    relative = path.relative_to(_APP_ROOT.parent).with_suffix("")
    package = ".".join(relative.parts[:-1])
    return list(imported_modules(path.read_text(encoding="utf-8"), package=package))


def _matches(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(f"{prefix}.")


def test_readiness_core_is_broker_and_transport_neutral() -> None:
    offending: dict[str, list[str]] = {}
    for path in sorted((_APP_ROOT / "strategy_readiness").rglob("*.py")):
        bad = [m for m in _modules(path) for f in _FORBIDDEN_IN_READINESS if _matches(m, f)]
        if bad:
            offending[str(path)] = sorted(set(bad))
    assert offending == {}, f"strategy_readiness broke its neutral boundary: {offending}"


def test_domain_layers_do_not_import_readiness() -> None:
    offenders: dict[str, list[str]] = {}
    for layer in _READINESS_UNAWARE_LAYERS:
        for path in sorted(layer.rglob("*.py")):
            hits = [m for m in _modules(path) if _matches(m, "app.strategy_readiness")]
            if hits:
                offenders[str(path)] = sorted(set(hits))
    assert offenders == {}, f"readiness leaked into a readiness-unaware layer: {offenders}"


def test_readiness_engine_is_not_constructed_by_composition() -> None:
    """Phase G is inert: no production module builds the readiness engine."""
    constructors: list[str] = []
    for path in sorted(_APP_ROOT.rglob("*.py")):
        if path.is_relative_to(_APP_ROOT / "strategy_readiness"):
            continue
        if "StrategyReadinessEngine(" in path.read_text(encoding="utf-8"):
            constructors.append(str(path))
    assert constructors == [], f"readiness engine constructed in composition: {constructors}"
