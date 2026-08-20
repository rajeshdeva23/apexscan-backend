"""AST import-boundary policy for the Strategy Engine and Strategy Manager (P5.0).

Enforces ADR-007 / docs/07 layering *before* strategy code lands:

- Strategies are pure, read-only fact consumers: they may import canonical schemas,
  their own package's shared contracts, and a small allowlist of *read-only* Market
  Engine value/fact modules — never a provider adapter, a broker/DB/transport SDK,
  the event bus, the API/WebSocket/cache layers, the Strategy Manager, or the Market
  Engine's mutation engines (docs/07 §4.11; ADR-007 D8/D14).
- The Strategy Manager orchestrates: it may additionally import the event bus and the
  requirement-registry seam — but never a concrete provider or a concrete strategy
  implementation (Open-Closed; docs/07 rules 29/30, ADR-007 D8/D10).
- Concrete strategy implementations never import each other (isolation; docs/07 §4.2).

The AST resolver (:func:`imported_modules`, :func:`_matches`) is reused from the
Phase-4 :mod:`tests.architecture.import_boundary` — one parser, no duplication.
"""

from __future__ import annotations

from pathlib import Path

from tests.architecture.import_boundary import _matches, imported_modules

# Concrete I/O / provider / transport / persistence SDKs no Strategy-layer code
# may touch (facts arrive via the read-only MarketContext).
_FORBIDDEN_EXTERNAL: tuple[str, ...] = (
    "httpx",
    "websockets",
    "pyotp",
    "dhanhq",
    "redis",
    "sqlalchemy",
    "aiohttp",
    "requests",
)

# Read-only Market Engine value/fact modules a strategy may consume. The Market
# Engine *package root* and its mutation engines (candle_engine, tick_engine, state,
# session, events, historical.service/coordinator/cache/source/reconciliation, …)
# are deliberately excluded — strategies import precise contract modules, never the
# kitchen-sink root (ADR-007 D8; docs/07 §4.11).
_READONLY_MARKET_ENGINE: tuple[str, ...] = (
    "app.market_engine.context",
    "app.market_engine.timeframe",
    "app.market_engine.historical.context",
    "app.market_engine.historical.requirements",
)

_STRATEGY_ALLOWED_APP: tuple[str, ...] = (
    "app.schemas",
    "app.strategies",
    *_READONLY_MARKET_ENGINE,
)

_MANAGER_ALLOWED_APP: tuple[str, ...] = (
    "app.schemas",
    "app.strategies",
    "app.strategy_manager",
    "app.events",
    "app.core",
    "app.market_engine.events",
    *_READONLY_MARKET_ENGINE,
)

# The Strategy Manager core must never import a concrete strategy implementation
# (Open-Closed; composition/startup wiring may, outside the manager core).
_CONCRETE_IMPLEMENTATIONS: tuple[str, ...] = ("app.strategies.implementations",)


def _violations(
    source: str,
    *,
    package: str,
    allowed_app: tuple[str, ...],
    denied_app: tuple[str, ...],
) -> list[str]:
    """Return imports in ``source`` that break the given layer policy.

    An ``app.*`` import violates if it matches ``denied_app`` (override) or does not
    match ``allowed_app``. An external import violates only if it matches the shared
    forbidden-SDK list.

    Args:
        source: Python source text.
        package: The dotted package the source belongs to (resolves relative imports).
        allowed_app: Permitted ``app.*`` prefixes.
        denied_app: ``app.*`` prefixes forbidden even if under an allowed prefix.

    Returns:
        A sorted, de-duplicated list of offending dotted module names.
    """
    violations: set[str] = set()
    for name in imported_modules(source, package=package):
        if name == "app" or name.startswith("app."):
            if _matches(name, denied_app) or not _matches(name, allowed_app):
                violations.add(name)
        elif _matches(name, _FORBIDDEN_EXTERNAL):
            violations.add(name)
    return sorted(violations)


def strategy_violations(source: str, *, package: str) -> list[str]:
    """Return Strategy-layer import-boundary violations in ``source``."""
    return _violations(source, package=package, allowed_app=_STRATEGY_ALLOWED_APP, denied_app=())


def manager_violations(source: str, *, package: str) -> list[str]:
    """Return Strategy-Manager import-boundary violations in ``source``.

    Concrete strategy implementations are denied even though ``app.strategies`` is
    otherwise allowed (Open-Closed).
    """
    return _violations(
        source,
        package=package,
        allowed_app=_MANAGER_ALLOWED_APP,
        denied_app=_CONCRETE_IMPLEMENTATIONS,
    )


def _implementation_id(dotted: str) -> str | None:
    """Return the implementation id if ``dotted`` is under ``…implementations.<id>``."""
    prefix = "app.strategies.implementations."
    if not dotted.startswith(prefix):
        return None
    remainder = dotted[len(prefix) :]
    return remainder.split(".", 1)[0] or None


def cross_implementation_violations(source: str, *, package: str) -> list[str]:
    """Return imports of a *different* concrete strategy implementation.

    A strategy implementation module may import within its own implementation subtree
    but never a sibling implementation (isolation, docs/07 §4.2). Modules outside
    ``app.strategies.implementations`` are unaffected by this rule.
    """
    own = _implementation_id(package)
    violations: set[str] = set()
    for name in imported_modules(source, package=package):
        other = _implementation_id(name)
        if other is not None and other != own:
            violations.add(name)
    return sorted(violations)


def scan_strategies(app_root: Path) -> dict[str, list[str]]:
    """Scan every module under ``app/strategies`` for Strategy-layer violations."""
    project_root = app_root.parent
    results: dict[str, list[str]] = {}
    for path in sorted((app_root / "strategies").rglob("*.py")):
        relative = path.relative_to(project_root).with_suffix("")
        package = ".".join(relative.parts[:-1])
        source = path.read_text(encoding="utf-8")
        offenders = strategy_violations(source, package=package)
        offenders += cross_implementation_violations(source, package=package)
        if offenders:
            results[str(path)] = sorted(set(offenders))
    return results


def scan_strategy_manager(app_root: Path) -> dict[str, list[str]]:
    """Scan every module under ``app/strategy_manager`` for manager violations."""
    project_root = app_root.parent
    results: dict[str, list[str]] = {}
    for path in sorted((app_root / "strategy_manager").rglob("*.py")):
        relative = path.relative_to(project_root).with_suffix("")
        package = ".".join(relative.parts[:-1])
        violations = manager_violations(path.read_text(encoding="utf-8"), package=package)
        if violations:
            results[str(path)] = violations
    return results
