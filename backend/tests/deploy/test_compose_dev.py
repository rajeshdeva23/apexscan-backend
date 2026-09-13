"""Dev/default Compose topology for the market-ingestion producer (DECOUPLING PHASE H3C).

Asserts ``docker-compose.yml`` keeps the decoupled ``market-ingestion`` service inert-by-default
and correctly packaged: it is gated behind the ``market-ingestion`` profile (the default stack
never starts it), exposes no public port, depends only on Redis (never Postgres), runs the
dedicated module entrypoint, never auto-restarts, and keeps the daemon-global unique container name
that enforces the single-instance guarantee (ADR-026). A Docker-capable CI additionally renders
``docker compose config`` for both the default and profile views; this structural check enforces
the same invariants deterministically everywhere.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE = yaml.safe_load((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
_SERVICES = _COMPOSE["services"]
_INGESTION = _SERVICES["market-ingestion"]


def test_default_stack_excludes_market_ingestion() -> None:
    # Every profile-gated service is excluded from `docker compose up` without --profile; the
    # market-ingestion service carries a profile, so the default dev stack never starts it.
    assert _INGESTION["profiles"] == ["market-ingestion"]
    default_services = [name for name, svc in _SERVICES.items() if "profiles" not in svc]
    assert "market-ingestion" not in default_services
    assert set(default_services) == {"postgres", "redis", "backend"}


def test_market_ingestion_profile_includes_it() -> None:
    assert "market-ingestion" in _INGESTION["profiles"]


def test_market_ingestion_has_no_public_port() -> None:
    assert "ports" not in _INGESTION  # never internet-reachable (producer-only, ADR-026)


def test_market_ingestion_depends_on_redis_only() -> None:
    depends_on = _INGESTION["depends_on"]
    assert set(depends_on) == {"redis"}  # Redis present, Postgres absent
    assert depends_on["redis"]["condition"] == "service_healthy"


def test_market_ingestion_runs_the_module_entrypoint() -> None:
    assert _INGESTION["command"] == ["python", "-m", "app.market_ingestion"]


def test_market_ingestion_does_not_auto_restart() -> None:
    assert _INGESTION["restart"] == "no"  # a failed producer stays down; no crash-loop


def test_market_ingestion_singleton_container_name() -> None:
    # A daemon-global unique container name blocks a second instance / --scale (ADR-026 singleton).
    assert _INGESTION["container_name"] == "apexscan-market-ingestion"


def test_market_ingestion_shares_the_backend_image_build() -> None:
    assert _INGESTION["build"]["context"] == "./backend"
    assert _INGESTION["build"]["dockerfile"] == "Dockerfile"
