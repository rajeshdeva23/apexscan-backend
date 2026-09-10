"""Compose safety: dev builds from source; production consumes immutable images.

Guards §19/§20 — the developer ``docker compose up --build`` primitive can never
be the production path, and the production authority
(``docker-compose.production.yml``) never builds or bind-mounts source.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]


def _text(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _directives(rel: str) -> str:
    """File text with comment and blank lines removed (assert on real directives)."""
    lines = _text(rel).splitlines()
    return "\n".join(line for line in lines if line.strip() and not line.strip().startswith("#"))


def test_legacy_prod_overlay_is_retired() -> None:
    assert not (_ROOT / "docker-compose.prod.yml").exists()


def test_dev_script_never_uses_production_authority() -> None:
    dev = _text("scripts/dev.sh")
    assert "docker compose up --build" in dev
    assert "docker-compose.production.yml" not in dev  # dev never touches production
    assert "docker-compose.prod.yml" not in dev


def test_remote_update_pulls_immutable_and_never_builds() -> None:
    script = _directives("scripts/deploy/remote_update.sh")
    assert "docker-compose.production.yml" in script
    assert "-p apexscan" in script  # project identity pinned
    assert "--no-deps" in script and "--no-build" in script
    assert "--build" not in script.replace("--no-build", "")  # only --no-build, never --build
    assert "alembic" not in script  # no migrations in the deploy path
    assert "APEXSCAN_IMAGE" in script
