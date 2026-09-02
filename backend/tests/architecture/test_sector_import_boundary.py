"""ADR-016 import-boundary guard for the Market Intelligence / Sector context.

Dependency direction is one-way (market pipeline -> market_intelligence -> strategies).
Sector code must never import the strategy layer, the strategy manager, a concrete
provider adapter, or any DB/Redis/transport SDK — it is pure reference/domain logic.
Reuses the Phase-4 AST resolver, no text matching.
"""

from __future__ import annotations

from pathlib import Path

from tests.architecture.import_boundary import _matches, imported_modules

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"

# app.* prefixes the sector context MAY import (canonical vocabulary + itself).
_ALLOWED_APP: tuple[str, ...] = ("app.schemas", "app.market_intelligence")

# app.* prefixes forbidden by ADR-016 dependency direction / persistence rules.
_DENIED_APP: tuple[str, ...] = (
    "app.strategies",
    "app.strategy_manager",
    "app.api",
    "app.adapters.dhan",
)

# I/O / provider / persistence SDKs no sector runtime module may touch. (generate.py
# is a build-time tool that imports the repo's own normalizer only; still SDK-free.)
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


def _violations(source: str, *, package: str) -> list[str]:
    found: set[str] = set()
    for name in imported_modules(source, package=package):
        if name == "app" or name.startswith("app."):
            if _matches(name, _DENIED_APP) or not _matches(name, _ALLOWED_APP):
                found.add(name)
        elif _matches(name, _FORBIDDEN_EXTERNAL):
            found.add(name)
    return sorted(found)


def test_sector_context_respects_dependency_direction() -> None:
    project_root = _APP_ROOT.parent
    results: dict[str, list[str]] = {}
    for path in sorted((_APP_ROOT / "market_intelligence").rglob("*.py")):
        if path.name == "generate.py":  # build-time tool; reproduces universe via repo derivation
            continue
        relative = path.relative_to(project_root).with_suffix("")
        package = ".".join(relative.parts[:-1])
        offenders = _violations(path.read_text(encoding="utf-8"), package=package)
        if offenders:
            results[str(path)] = offenders
    assert results == {}, f"sector import-boundary violations: {results}"
