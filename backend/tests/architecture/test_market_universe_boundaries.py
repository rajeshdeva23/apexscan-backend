"""Import-boundary + inertness proofs for the dynamic universe package (DECOUPLING PHASE E).

The UniverseSnapshot authority is a separate broker-neutral layer: the Dhan adapter, backend
runtime, strategy/scanner, sector math, and IPC transport neither own nor import it, and it is not
constructed by production composition. It may reuse the sector authority and the trading calendar.
Canonical MarketData never gains a provider security id.
"""

from __future__ import annotations

from pathlib import Path

from tests.architecture.import_boundary import imported_modules

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"

# market_universe core must not depend on transport/provider/strategy/composition layers.
_FORBIDDEN_IN_UNIVERSE = (
    "app.adapters.dhan",
    "app.market_ipc",
    "app.strategies",
    "app.strategy_manager",
    "app.services",
    "websockets",
    "pyotp",
)

# Layers that must NOT import the universe package (one-directional dependency).
_UNIVERSE_UNAWARE_LAYERS = (
    _APP_ROOT / "adapters",
    _APP_ROOT / "market_intelligence" / "sector",
    _APP_ROOT / "strategies",
    _APP_ROOT / "strategy_manager",
    _APP_ROOT / "market_ipc",
    _APP_ROOT / "services",
)


def _modules(path: Path) -> list[str]:
    relative = path.relative_to(_APP_ROOT.parent).with_suffix("")
    package = ".".join(relative.parts[:-1])
    return list(imported_modules(path.read_text(encoding="utf-8"), package=package))


def _matches(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(f"{prefix}.")


def test_universe_core_is_broker_and_layer_neutral() -> None:
    offending: dict[str, list[str]] = {}
    for path in sorted((_APP_ROOT / "market_universe").rglob("*.py")):
        bad = [m for m in _modules(path) for f in _FORBIDDEN_IN_UNIVERSE if _matches(m, f)]
        if bad:
            offending[str(path)] = sorted(set(bad))
    assert offending == {}, f"market_universe broke its neutral boundary: {offending}"


def test_domain_layers_do_not_import_universe() -> None:
    offenders: dict[str, list[str]] = {}
    for layer in _UNIVERSE_UNAWARE_LAYERS:
        for path in sorted(layer.rglob("*.py")):
            hits = [m for m in _modules(path) if _matches(m, "app.market_universe")]
            if hits:
                offenders[str(path)] = sorted(set(hits))
    assert offenders == {}, f"universe leaked into a universe-unaware layer: {offenders}"


def test_canonical_marketdata_has_no_provider_security_id() -> None:
    source = (_APP_ROOT / "schemas" / "market_data.py").read_text(encoding="utf-8")
    assert "security_id" not in source  # provider ids stay in reference mapping metadata only


def test_universe_is_not_constructed_by_composition() -> None:
    """Phase E is inert: no production module builds the resolver/store/snapshot."""
    inert = ("UniverseResolver(", "FileUniverseSnapshotStore(", "UniverseSnapshot(")
    constructors: dict[str, list[str]] = {}
    for path in sorted(_APP_ROOT.rglob("*.py")):
        if path.is_relative_to(_APP_ROOT / "market_universe"):
            continue
        text = path.read_text(encoding="utf-8")
        hits = [name for name in inert if name in text]
        if hits:
            constructors[str(path)] = hits
    assert constructors == {}, f"universe constructed in production composition: {constructors}"
