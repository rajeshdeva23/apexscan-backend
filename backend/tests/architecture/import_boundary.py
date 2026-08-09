"""AST-based import-boundary checker for the Market Engine.

The Market Engine is broker-blind and strategy-blind: it consumes only canonical
contracts and its permitted backend seams, and must never import a concrete
broker adapter, a broker/transport SDK, strategy code, the API/frontend layer,
or durable-persistence modules (docs/03 §3.6, docs/06 §28.9-10, ADR-003; the
engine "owns no durable table", docs/02 §7).

Imports are analysed with the ``ast`` module rather than text matching, so the
check is insensitive to import ordering, aliasing, and formatting.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

# Internal (``app.*``) prefixes the Market Engine MAY depend on: the adapter
# *contract* (not concrete adapters), cache, core, and events per docs/03 §3.6,
# plus the canonical schemas boundary and the engine's own package.
ALLOWED_APP_PREFIXES: tuple[str, ...] = (
    "app.schemas",
    "app.adapters.base",
    "app.cache",
    "app.core",
    "app.events",
    "app.market_engine",
)

# External packages that embody broker/transport concerns the engine must never
# import (broker-blindness; the WebSocket transport and Dhan auth live only in
# the Data Provider adapter).
FORBIDDEN_EXTERNAL_PREFIXES: tuple[str, ...] = (
    "websockets",
    "pyotp",
)


def _matches(name: str, prefixes: tuple[str, ...]) -> bool:
    """Return whether ``name`` equals or is a dotted child of any prefix."""
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)


def _resolve_from_import(node: ast.ImportFrom, package: str) -> str:
    """Resolve an ``ImportFrom`` node to an absolute dotted module name."""
    if node.level == 0:
        return node.module or ""
    parts = package.split(".") if package else []
    kept = parts[: len(parts) - (node.level - 1)]
    prefix = ".".join(kept)
    if node.module:
        return f"{prefix}.{node.module}" if prefix else node.module
    return prefix


def imported_modules(source: str, *, package: str) -> Iterator[str]:
    """Yield the absolute dotted module of every import statement in ``source``.

    Args:
        source: Python source text.
        package: The dotted package the source belongs to, used to resolve
            relative imports to absolute names.

    Yields:
        Absolute dotted module names (empty strings are not yielded).
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    yield alias.name
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_from_import(node, package)
            if resolved:
                yield resolved


def forbidden_imports(source: str, *, package: str) -> list[str]:
    """Return the sorted, de-duplicated imports in ``source`` that break the boundary.

    An ``app.*`` import is a violation unless it matches an allowed prefix; an
    external import is a violation only if it matches a forbidden prefix.

    Args:
        source: Python source text to analyse.
        package: The dotted package the source belongs to.

    Returns:
        A sorted list of offending dotted module names (empty when compliant).
    """
    violations: set[str] = set()
    for name in imported_modules(source, package=package):
        if name == "app" or name.startswith("app."):
            if not _matches(name, ALLOWED_APP_PREFIXES):
                violations.add(name)
        elif _matches(name, FORBIDDEN_EXTERNAL_PREFIXES):
            violations.add(name)
    return sorted(violations)


def scan_market_engine(app_root: Path) -> dict[str, list[str]]:
    """Scan every module under ``app/market_engine`` for boundary violations.

    Args:
        app_root: Path to the ``app`` package directory.

    Returns:
        A mapping of offending file path (str) to its list of violations; an
        empty mapping means the package is fully compliant.
    """
    project_root = app_root.parent
    results: dict[str, list[str]] = {}
    for path in sorted((app_root / "market_engine").rglob("*.py")):
        relative = path.relative_to(project_root).with_suffix("")
        package = ".".join(relative.parts[:-1])
        violations = forbidden_imports(path.read_text(encoding="utf-8"), package=package)
        if violations:
            results[str(path)] = violations
    return results
