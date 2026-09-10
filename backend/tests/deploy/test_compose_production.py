"""Production Compose authority models the exact production topology (DEPLOY-3B).

Asserts the reviewed ``docker-compose.production.yml`` preserves the verified
production identities (project, named volumes, network, container names, external
env paths, loopback backend, no public DB/Redis) and changes only the backend
image to the immutable ``${APEXSCAN_IMAGE}`` — never a build, dev bind mount, or
mutable tag. Every host resource is an absolute path or named volume, so the file
is release-directory independent.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]
_PROD = yaml.safe_load((_ROOT / "docker-compose.production.yml").read_text(encoding="utf-8"))
_SERVICES = _PROD["services"]


def test_project_identity_is_apexscan() -> None:
    assert _PROD["name"] == "apexscan"


def test_named_volumes_preserved() -> None:
    assert _PROD["volumes"]["postgres_data"]["name"] == "apexscan_postgres_data"
    assert _PROD["volumes"]["redis_data"]["name"] == "apexscan_redis_data"


def test_network_identity_preserved() -> None:
    assert _PROD["networks"]["apexscan-net"]["name"] == "apexscan-net"


def test_backend_uses_immutable_image_variable_and_no_build() -> None:
    backend = _SERVICES["backend"]
    # ${APEXSCAN_IMAGE:?...} fails closed at compose time when unset/empty.
    assert backend["image"].startswith("${APEXSCAN_IMAGE:?")
    assert "build" not in backend  # never builds from source
    assert ":latest" not in backend["image"] and ":edge" not in backend["image"]


def test_backend_has_no_dev_source_bind_mount() -> None:
    volumes = _SERVICES["backend"].get("volumes", [])
    assert "./backend:/app" not in volumes
    assert volumes == ["/opt/apexscan/artifacts:/app/artifacts"]  # only the artifacts bind


def test_backend_loopback_only_binding() -> None:
    assert _SERVICES["backend"]["ports"] == ["127.0.0.1:8000:8000"]
    assert _SERVICES["backend"]["container_name"] == "apexscan-backend"


def test_backend_external_env_files_only() -> None:
    env_files = _SERVICES["backend"]["env_file"]
    assert env_files == [
        "/etc/apexscan/apexscan-infra.env",
        "/etc/apexscan/backend.env",
        "/etc/apexscan/dhan.env",
    ]
    assert all(p.startswith("/etc/apexscan/") for p in env_files)


def test_backend_depends_on_data_services_and_healthcheck() -> None:
    backend = _SERVICES["backend"]
    assert set(backend["depends_on"]) == {"postgres", "redis"}
    assert backend["restart"] == "unless-stopped"
    assert "healthcheck" in backend


def test_backend_production_flags_preserved() -> None:
    env = _SERVICES["backend"]["environment"]
    assert "SESSION_OHLC_EVIDENCE_OBSERVER_ENABLED=true" in env
    assert "SECTOR_SHADOW_ENABLED=true" in env


def test_postgres_preserved_and_not_public() -> None:
    pg = _SERVICES["postgres"]
    assert pg["image"] == "postgres:17-alpine"
    assert pg["container_name"] == "apexscan-postgres"
    assert "ports" not in pg  # never internet-reachable
    assert "postgres_data:/var/lib/postgresql/data" in pg["volumes"]
    assert pg["env_file"] == ["/etc/apexscan/postgres.env"]


def test_redis_preserved_and_not_public() -> None:
    redis = _SERVICES["redis"]
    assert redis["image"] == "redis:7-alpine"
    assert redis["container_name"] == "apexscan-redis"
    assert "ports" not in redis  # never internet-reachable
    assert "redis_data:/data" in redis["volumes"]


def test_release_directory_independent_paths() -> None:
    # Every host path referenced must be absolute (or a named volume); a relative
    # bind mount would resolve differently from /opt/apexscan/releases/<sha>/.
    for service in _SERVICES.values():
        for mount in service.get("volumes", []):
            host = mount.split(":", 1)[0]
            assert host.startswith("/") or host.isidentifier() or "_" in host, host
        for env in service.get("env_file", []):
            assert env.startswith("/"), env


def test_no_committed_secret_values() -> None:
    text = (_ROOT / "docker-compose.production.yml").read_text(encoding="utf-8").lower()
    for secret in ("password=", "token=", "totp", "secret=", "pin="):
        assert secret not in text
