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


def test_shadow_consumer_is_not_constructed_by_composition() -> None:
    """Phase C is shadow-only: no production module (outside market_ipc) may build the consumer.

    The Redis consumer must be reachable only through explicit test/offline composition — never
    wired into backend startup — so merging Phase C activates no consumer and does not begin the
    (forbidden) Redis -> backend -> TickEngine cutover.
    """
    constructors: list[str] = []
    for path in sorted(_APP_ROOT.rglob("*.py")):
        if path.is_relative_to(_APP_ROOT / "market_ipc"):
            continue
        if "MarketEventConsumer(" in path.read_text(encoding="utf-8"):
            constructors.append(str(path))
    assert constructors == [], f"shadow consumer constructed in composition: {constructors}"


def test_reference_recovery_is_not_constructed_by_composition() -> None:
    """Phase D is inert: no production module builds the reference writer/loader/store.

    Compacted reference recovery must be reachable only through explicit test/offline
    composition, so merging Phase D activates no Redis writes or loads.
    """
    inert = ("ReferenceStateWriter(", "ReferenceStateLoader(", "RedisCompactedReferenceStore(")
    constructors: dict[str, list[str]] = {}
    for path in sorted(_APP_ROOT.rglob("*.py")):
        if path.is_relative_to(_APP_ROOT / "market_ipc"):
            continue
        text = path.read_text(encoding="utf-8")
        hits = [name for name in inert if name in text]
        if hits:
            constructors[str(path)] = hits
    assert constructors == {}, f"reference recovery constructed in composition: {constructors}"


def test_consumer_runtime_is_not_wired_into_startup() -> None:
    """H4A is offline: no production module (outside market_ipc) builds or composes the runtime.

    The shadow consumer runtime must be reachable only through explicit test/offline composition,
    so merging H4A activates no backend consumer and cannot begin the (forbidden) Redis -> backend
    -> TickEngine cutover.
    """
    seams = ("MarketEventConsumerRuntime(", "compose_consumer_runtime(")
    wired: dict[str, list[str]] = {}
    for path in sorted(_APP_ROOT.rglob("*.py")):
        if path.is_relative_to(_APP_ROOT / "market_ipc"):
            continue
        text = path.read_text(encoding="utf-8")
        hits = [name for name in seams if name in text]
        if hits:
            wired[str(path)] = hits
    assert wired == {}, f"consumer runtime wired into composition: {wired}"


def test_consumer_runtime_imports_no_producer_or_provider_surface() -> None:
    """The shadow consumer runtime pulls no Dhan provider, IPC publisher, or producer-epoch code.

    Proves H4A composes only the consume path: no live provider is created, the publisher is not
    activated, and no producer epoch is allocated by the runtime.
    """
    forbidden = ("app.adapters.dhan", "app.market_ipc.publisher", "app.market_ipc.epoch")
    modules = _modules(_APP_ROOT / "market_ipc" / "consumer_runtime.py")
    offending = sorted({m for m in modules for f in forbidden if _matches(m, f)})
    assert offending == [], f"consumer runtime imported producer/provider surface: {offending}"


def test_shadow_compare_imports_no_authority_redis_or_provider_surface() -> None:
    """The H4C comparator is a pure canonical-value function: no Redis, authority, or provider.

    Proves the offline parity path holds no Redis client, never reaches the production TickEngine
    / MarketContext / market engine / strategies, and pulls in no Dhan provider or IPC publisher —
    so it can compare canonical events but can never mutate authoritative state or contact a broker.
    """
    forbidden = (
        "redis",
        "app.market_engine",
        "app.market_intelligence",
        "app.adapters.dhan",
        "app.strategies",
        "app.strategy_manager",
        "app.services",
        "app.market_ipc.publisher",
        "app.market_ipc.epoch",
    )
    modules = _modules(_APP_ROOT / "market_ipc" / "shadow_compare.py")
    offending = sorted({m for m in modules for f in forbidden if _matches(m, f)})
    assert offending == [], f"shadow_compare imported forbidden surface: {offending}"


def test_h5_offline_topology_touches_no_authority_broker_or_fix_surface() -> None:
    """H5 (T21-T24): the offline end-to-end topology imports no authority, broker, or FIX surface.

    Proves the composed producer+consumer parity pipeline wires only the ingestion producer
    composition and the shadow consumer/comparator — never the TickEngine/MarketContext authority,
    a strategy/session/trading path, a Dhan broker adapter, or a FIX/WebSocket transport.
    """
    forbidden = (
        "app.adapters.dhan",
        "app.market_engine",
        "app.market_intelligence",
        "app.strategies",
        "app.strategy_manager",
        "pyotp",
        "websockets",
    )
    path = _APP_ROOT.parent / "tests" / "integration" / "test_market_ipc_h5_offline_e2e_redis.py"
    offending = sorted({m for m in _modules(path) for f in forbidden if _matches(m, f)})
    assert offending == [], f"H5 offline topology imported forbidden surface: {offending}"


def test_h6_backend_restart_topology_touches_no_authority_broker_or_fix_surface() -> None:
    """H6 (T25-T29): the backend-restart topology imports no authority, broker, or FIX surface.

    Proves the continuously-alive ingestion + restarted-consumer proof wires only the ingestion
    producer composition and the shadow consumer/comparator — never the TickEngine/MarketContext
    authority, a strategy/session/trading path, a Dhan broker adapter, or a FIX/WebSocket transport.
    """
    forbidden = (
        "app.adapters.dhan",
        "app.market_engine",
        "app.market_intelligence",
        "app.strategies",
        "app.strategy_manager",
        "pyotp",
        "websockets",
    )
    integration = _APP_ROOT.parent / "tests" / "integration"
    path = integration / "test_market_ipc_h6_backend_restart_redis.py"
    offending = sorted({m for m in _modules(path) for f in forbidden if _matches(m, f)})
    assert offending == [], f"H6 backend-restart topology imported forbidden surface: {offending}"
