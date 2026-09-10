"""Compose safety: dev builds from source, production consumes immutable images.

Guards §19 — the developer ``docker compose up --build`` primitive can never be
the production path, and the production overlay never builds or bind-mounts
source.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]


def _text(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _directives(rel: str) -> str:
    """File text with comment and blank lines removed (assert on real directives)."""
    lines = _text(rel).splitlines()
    return "\n".join(line for line in lines if line.strip() and not line.strip().startswith("#"))


def test_base_compose_is_developer_build() -> None:
    base = yaml.safe_load(_text("docker-compose.yml"))
    backend = base["services"]["backend"]
    assert "build" in backend  # dev builds from local source
    assert "./backend:/app" in backend["volumes"]  # dev source bind-mount


def test_prod_overlay_consumes_immutable_image_only() -> None:
    # The overlay uses Compose's !reset tag (not valid for safe_load), so assert
    # on its text contract.
    overlay = _directives("docker-compose.prod.yml")
    assert "image: ${APEXSCAN_IMAGE" in overlay
    assert "build: !reset null" in overlay  # base build dropped
    assert "volumes: !reset []" in overlay  # base source bind-mount dropped
    assert "context:" not in overlay  # never builds from source
    assert "--build" not in overlay


def test_dev_script_never_uses_production_overlay() -> None:
    dev = _text("scripts/dev.sh")
    assert "docker compose up --build" in dev
    assert "docker-compose.prod.yml" not in dev  # dev never touches the prod overlay


def test_remote_update_pulls_immutable_and_never_builds() -> None:
    script = _directives("scripts/deploy/remote_update.sh")
    assert "docker-compose.prod.yml" in script
    assert "--no-build" in script
    assert "--build" not in script.replace("--no-build", "")  # only --no-build, never --build
    assert "alembic" not in script  # no migrations in the deploy path
    assert "APEXSCAN_IMAGE" in script
