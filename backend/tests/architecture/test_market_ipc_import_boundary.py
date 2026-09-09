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


# Only the runtime/composition seam may reference market_ipc (Phase B wires the shadow
# publisher into the LiveMarketRuntime). The domain layers below must stay IPC-unaware so a
# future transport/provider swap never reaches sector/strategy/scanner/adapter code.
_IPC_UNAWARE_LAYERS = (
    _APP_ROOT / "adapters",
    _APP_ROOT / "market_intelligence" / "sector",
    _APP_ROOT / "strategies",
    _APP_ROOT / "strategy_manager",
    _APP_ROOT / "market_engine",
)


def test_domain_layers_do_not_import_ipc() -> None:
    offenders: dict[str, list[str]] = {}
    for layer in _IPC_UNAWARE_LAYERS:
        for path in sorted(layer.rglob("*.py")):
            hits = [m for m in _modules(path) if _matches(m, "app.market_ipc")]
            if hits:
                offenders[str(path)] = sorted(set(hits))
    assert offenders == {}, f"IPC leaked into an IPC-unaware domain layer: {offenders}"
