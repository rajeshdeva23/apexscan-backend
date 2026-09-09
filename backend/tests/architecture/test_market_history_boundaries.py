"""Import-boundary + inertness proofs for the historical package (DECOUPLING PHASE F).

market_history is broker-neutral historical reference data: it reuses the trading calendar but
imports no Dhan adapter / IPC transport / strategy / scanner / trading code, no domain layer
imports it, and production composition never constructs the historical service (Phase G owns
readiness integration).
"""

from __future__ import annotations

from pathlib import Path

from tests.architecture.import_boundary import imported_modules

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"

_FORBIDDEN_IN_HISTORY = (
    "app.adapters.dhan",
    "app.market_ipc",
    "app.market_universe",
    "app.strategies",
    "app.strategy_manager",
    "app.services",
    "websockets",
    "pyotp",
)

_HISTORY_UNAWARE_LAYERS = (
    _APP_ROOT / "adapters",
    _APP_ROOT / "market_intelligence" / "sector",
    _APP_ROOT / "strategies",
    _APP_ROOT / "strategy_manager",
    _APP_ROOT / "market_ipc",
    _APP_ROOT / "market_universe",
    _APP_ROOT / "services",
)


def _modules(path: Path) -> list[str]:
    relative = path.relative_to(_APP_ROOT.parent).with_suffix("")
    package = ".".join(relative.parts[:-1])
    return list(imported_modules(path.read_text(encoding="utf-8"), package=package))


def _matches(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(f"{prefix}.")


def test_history_core_is_broker_and_layer_neutral() -> None:
    offending: dict[str, list[str]] = {}
    for path in sorted((_APP_ROOT / "market_history").rglob("*.py")):
        bad = [m for m in _modules(path) for f in _FORBIDDEN_IN_HISTORY if _matches(m, f)]
        if bad:
            offending[str(path)] = sorted(set(bad))
    assert offending == {}, f"market_history broke its neutral boundary: {offending}"


def test_domain_layers_do_not_import_history() -> None:
    offenders: dict[str, list[str]] = {}
    for layer in _HISTORY_UNAWARE_LAYERS:
        for path in sorted(layer.rglob("*.py")):
            hits = [m for m in _modules(path) if _matches(m, "app.market_history")]
            if hits:
                offenders[str(path)] = sorted(set(hits))
    assert offenders == {}, f"history leaked into a history-unaware layer: {offenders}"


def test_history_service_is_not_constructed_by_composition() -> None:
    """Phase F is inert: no production module builds the historical service/store."""
    inert = ("HistoricalService(", "FileHistoricalBarStore(")
    constructors: dict[str, list[str]] = {}
    for path in sorted(_APP_ROOT.rglob("*.py")):
        if path.is_relative_to(_APP_ROOT / "market_history"):
            continue
        text = path.read_text(encoding="utf-8")
        hits = [name for name in inert if name in text]
        if hits:
            constructors[str(path)] = hits
    assert constructors == {}, f"historical service constructed in composition: {constructors}"
