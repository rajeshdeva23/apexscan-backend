"""Import-boundary guard for the passive sector shadow runtime (SECTOR-VIEW-1B).

The shadow runtime (``app.services.sector_intelligence``) is a read-only consumer of generic
domain + events + the SECTOR-2/3/4 engines. It must never import a concrete provider adapter,
Dhan auth, a transport/socket/HTTP SDK, DB/Redis persistence, the strategy layer, or any
order/execution/trading module. Analysed with the shared AST resolver, not text matching.
"""

from __future__ import annotations

from pathlib import Path

from tests.architecture.import_boundary import _matches, imported_modules

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"
_PACKAGE_DIR = _APP_ROOT / "services" / "sector_intelligence"

# The only app.* prefixes the shadow runtime may depend on: canonical vocabulary, the event
# bus, the generic Market Engine domain, the Market Intelligence engines, and itself.
_ALLOWED_APP: tuple[str, ...] = (
    "app.schemas",
    "app.events",
    "app.market_engine",
    "app.market_intelligence",
    "app.services.sector_intelligence",
)

# Provider / transport / persistence SDKs no shadow-runtime module may touch.
_FORBIDDEN_EXTERNAL: tuple[str, ...] = (
    "httpx",
    "websockets",
    "pyotp",
    "dhanhq",
    "redis",
    "sqlalchemy",
    "aiohttp",
    "requests",
    "socket",
)


def _violations(source: str, *, package: str) -> list[str]:
    found: set[str] = set()
    for name in imported_modules(source, package=package):
        if name == "app" or name.startswith("app."):
            if not _matches(name, _ALLOWED_APP):
                found.add(name)
        elif _matches(name, _FORBIDDEN_EXTERNAL):
            found.add(name)
    return sorted(found)


def test_sector_shadow_runtime_respects_dependency_direction() -> None:
    project_root = _APP_ROOT.parent
    results: dict[str, list[str]] = {}
    for path in sorted(_PACKAGE_DIR.rglob("*.py")):
        relative = path.relative_to(project_root).with_suffix("")
        package = ".".join(relative.parts[:-1])
        offenders = _violations(path.read_text(encoding="utf-8"), package=package)
        if offenders:
            results[str(path)] = offenders
    assert results == {}, f"sector shadow import-boundary violations: {results}"
