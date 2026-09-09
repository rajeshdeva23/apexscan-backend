"""Import-boundary proofs for the market_ipc IPC package (DECOUPLING PHASE A).

The IPC contracts must stay broker-neutral and free of sector/strategy/domain-coupling, and
Phase A must NOT be activated by application composition. These AST checks prove:

* market_ipc imports no Dhan adapter/protocol module (broker-neutral);
* market_ipc imports no sector math or strategy implementation;
* the canonical domain (``app.schemas.market_data``) does not depend on Redis or market_ipc;
* NO module in ``app/`` (outside market_ipc itself) imports ``app.market_ipc`` — composition
  does not construct or activate the new transport.
"""

from __future__ import annotations

from pathlib import Path

from tests.architecture.import_boundary import imported_modules

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"

_FORBIDDEN_IN_IPC = (
    "app.adapters.dhan",
    "app.market_intelligence.sector",
    "app.strategies",
    "app.strategy_manager",
    "app.services",
    "websockets",
    "pyotp",
)


def _modules(path: Path) -> list[str]:
    relative = path.relative_to(_APP_ROOT.parent).with_suffix("")
    package = ".".join(relative.parts[:-1])
    return list(imported_modules(path.read_text(encoding="utf-8"), package=package))


def _matches(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(f"{prefix}.")


def test_ipc_is_broker_and_domain_neutral() -> None:
    offending: dict[str, list[str]] = {}
    for path in sorted((_APP_ROOT / "market_ipc").rglob("*.py")):
        bad = [m for m in _modules(path) for f in _FORBIDDEN_IN_IPC if _matches(m, f)]
        if bad:
            offending[str(path)] = sorted(set(bad))
    assert offending == {}, f"market_ipc broke its broker/domain-neutral boundary: {offending}"


def test_canonical_domain_does_not_depend_on_transport() -> None:
    source = (_APP_ROOT / "schemas" / "market_data.py").read_text(encoding="utf-8")
    modules = list(imported_modules(source, package="app.schemas"))
    assert not any(_matches(m, "redis") for m in modules)
    assert not any(_matches(m, "app.market_ipc") for m in modules)


def test_application_composition_does_not_activate_ipc() -> None:
    activators: dict[str, list[str]] = {}
    for path in sorted(_APP_ROOT.rglob("*.py")):
        if path.is_relative_to(_APP_ROOT / "market_ipc"):
            continue
        hits = [m for m in _modules(path) if _matches(m, "app.market_ipc")]
        if hits:
            activators[str(path)] = sorted(set(hits))
    assert activators == {}, f"Phase A must not be wired into composition, but: {activators}"
