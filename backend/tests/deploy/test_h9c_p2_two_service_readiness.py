"""Two-service deployment + ownership configuration readiness (DECOUPLING PHASE H9C-P2).

Offline/static proofs for Gate F (both-service production deployment) and Gate I (production
ownership TTL/renewal configuration). No AWS, no Docker daemon, no Redis, no Dhan: the compose
authority and the deploy script are parsed as text/YAML, the preflight is a pure comparison, and
the ownership/inertness checks build real ``Settings`` with an isolated env.

Invariants proven:
* the production stack defines both application services from the SAME image, ingestion is a
  long-running-but-inert profile-gated service, and no compose env activates authority;
* the operator deploy script can target backend / market-ingestion / both, rejects an invalid
  target, and never mutates authority flags;
* the pre-activation preflight fails on a revision mismatch, an ownership-config mismatch, or any
  authority flag left ON — without leaking secret values;
* explicit production ownership config exists and preserves ``0 < renewal < ttl``;
* the ownership Redis client is bounded by a transport-level socket timeout (Gate I);
* a default/authority-OFF deployment acquires no ownership and builds no live provider.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.core.config import Settings
from app.market_ingestion.composition import compose_market_ingestion_service
from app.market_ingestion.mode import PhaseHFlags
from app.market_ingestion.ownership import OwnerRole, OwnershipLeaseConfig
from app.market_ingestion.ownership_runtime import build_provider_ownership_guard
from app.market_ingestion.service import MarketIngestionService
from deploy.two_service_preflight import (
    AUTHORITY_FLAG_KEYS,
    OWNERSHIP_PARITY_KEYS,
    _is_truthy,
    two_service_preflight,
    verify_authority_off,
    verify_ownership_config_parity,
    verify_same_revision,
)

_ROOT = Path(__file__).resolve().parents[3]
_PROD = yaml.safe_load((_ROOT / "docker-compose.production.yml").read_text(encoding="utf-8"))
_SERVICES = _PROD["services"]
_INGESTION = _SERVICES["market-ingestion"]
_BACKEND = _SERVICES["backend"]
_UPDATE_SH = (_ROOT / "scripts" / "deploy" / "remote_update.sh").read_text(encoding="utf-8")
_ENV_EXAMPLE = (_ROOT / ".env.example").read_text(encoding="utf-8")

_DIGEST = "ghcr.io/rajeshdeva23/apexscan-backend@sha256:" + "a" * 64
_OTHER_DIGEST = "ghcr.io/rajeshdeva23/apexscan-backend@sha256:" + "b" * 64
_GOOD_ENV = {
    "REDIS_URL": "redis://redis:6379/0",
    "MARKET_OWNERSHIP_ENABLED": "false",
    "MARKET_OWNERSHIP_LEASE_TTL_SECONDS": "30",
    "MARKET_OWNERSHIP_RENEWAL_INTERVAL_SECONDS": "10",
    "IPC_AUTHORITATIVE_ENABLED": "false",
}

_REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://apexscan:pw@postgres:5432/apexscan",
    "REDIS_URL": "redis://redis:6379/0",
}


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    for key, value in {**_REQUIRED_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _env_example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in _ENV_EXAMPLE.splitlines():
        line = raw.split("#", 1)[0].strip()
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


# =========================================================================== #
# §20 — production compose: both services, same image, long-running-but-inert
# =========================================================================== #
def test_production_defines_both_application_services() -> None:
    assert "backend" in _SERVICES
    assert "market-ingestion" in _SERVICES


def test_both_services_use_the_same_immutable_image_variable() -> None:
    assert _BACKEND["image"] == _INGESTION["image"]
    assert _INGESTION["image"].startswith("${APEXSCAN_IMAGE:?")  # digest-pinned, fail-closed
    assert "build" not in _INGESTION  # never builds from source


def test_ingestion_is_a_long_running_service() -> None:
    assert _INGESTION["restart"] == "unless-stopped"  # deployable as a persistent owner


def test_ingestion_stays_profile_gated_so_deployment_is_not_activation() -> None:
    # Profile gating is the explicit lifecycle control: the default up / deploy pipeline's
    # `up backend` never starts ingestion — deployment availability != provider activation.
    assert _INGESTION["profiles"] == ["market-ingestion"]
    default_services = [name for name, svc in _SERVICES.items() if "profiles" not in svc]
    assert "market-ingestion" not in default_services


def test_ingestion_runs_the_module_entrypoint_and_no_public_port() -> None:
    assert _INGESTION["command"] == ["python", "-m", "app.market_ingestion"]
    assert "ports" not in _INGESTION


def test_ingestion_reads_the_shared_infra_env_for_ownership_parity() -> None:
    # apexscan-infra.env is read by BOTH services → ownership/redis config lives there for parity.
    assert _INGESTION["env_file"] == [
        "/etc/apexscan/apexscan-infra.env",
        "/etc/apexscan/dhan.env",
    ]
    assert "/etc/apexscan/apexscan-infra.env" in _BACKEND["env_file"]


def test_compose_env_never_activates_authority() -> None:
    text = (_ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    for flag in ("MARKET_OWNERSHIP_ENABLED=true", "IPC_AUTHORITATIVE_ENABLED=true"):
        assert flag not in text
    for env in _INGESTION.get("environment", []):
        assert "MARKET_OWNERSHIP_ENABLED=true" not in env


# =========================================================================== #
# §21 — deploy script: target selection, invalid rejection, no flag mutation
# =========================================================================== #
def test_deploy_script_supports_backend_ingestion_and_both_targets() -> None:
    assert "backend)" in _UPDATE_SH
    assert "market-ingestion)" in _UPDATE_SH
    assert "both)" in _UPDATE_SH


def test_deploy_script_defaults_to_backend() -> None:
    assert 'TARGET="${1:-backend}"' in _UPDATE_SH


def test_deploy_script_rejects_invalid_target() -> None:
    assert "invalid target" in _UPDATE_SH
    assert "exit 2" in _UPDATE_SH


def test_deploy_script_uses_the_same_image_for_both_services() -> None:
    # A single ${APEXSCAN_IMAGE} drives both update paths — never two independent references.
    assert _UPDATE_SH.count("APEXSCAN_IMAGE must be set") == 1
    assert "up -d --no-deps --no-build backend" in _UPDATE_SH
    assert "up -d --no-deps --no-build market-ingestion" in _UPDATE_SH


def test_deploy_script_never_mutates_authority_flags() -> None:
    for token in ("MARKET_OWNERSHIP_ENABLED=", "IPC_AUTHORITATIVE_ENABLED="):
        assert token not in _UPDATE_SH  # deployment != activation


def test_deploy_script_only_touches_application_services_no_deps() -> None:
    # --no-deps keeps postgres/redis + volumes untouched during an app update.
    assert "--no-deps" in _UPDATE_SH
    assert "--no-build" in _UPDATE_SH


# =========================================================================== #
# §22 — pre-activation preflight (pure, secret-free)
# =========================================================================== #
def test_preflight_passes_on_matching_revision_config_and_authority_off() -> None:
    result = two_service_preflight(
        backend_image=_DIGEST,
        ingestion_image=_DIGEST,
        backend_env=dict(_GOOD_ENV),
        ingestion_env=dict(_GOOD_ENV),
    )
    assert result.ok
    assert result.reasons == ()


def test_preflight_fails_on_revision_mismatch() -> None:
    assert not verify_same_revision(_DIGEST, _OTHER_DIGEST).ok
    result = two_service_preflight(
        backend_image=_DIGEST,
        ingestion_image=_OTHER_DIGEST,
        backend_env=dict(_GOOD_ENV),
        ingestion_env=dict(_GOOD_ENV),
    )
    assert not result.ok
    assert any("image mismatch" in r for r in result.reasons)


@pytest.mark.parametrize(
    "key,value",
    [
        ("REDIS_URL", "redis://other:6379/0"),
        ("MARKET_OWNERSHIP_ENABLED", "true"),
        ("MARKET_OWNERSHIP_LEASE_TTL_SECONDS", "45"),
        ("MARKET_OWNERSHIP_RENEWAL_INTERVAL_SECONDS", "5"),
    ],
)
def test_preflight_fails_on_each_ownership_config_mismatch(key: str, value: str) -> None:
    ingestion = dict(_GOOD_ENV)
    ingestion[key] = value
    result = verify_ownership_config_parity(dict(_GOOD_ENV), ingestion)
    assert not result.ok
    assert any(key in r for r in result.reasons)


def test_preflight_fails_when_authority_flag_is_on() -> None:
    activated = {**_GOOD_ENV, "MARKET_OWNERSHIP_ENABLED": "true"}
    assert not verify_authority_off(activated, service="backend").ok
    assert not verify_authority_off(
        {**_GOOD_ENV, "IPC_AUTHORITATIVE_ENABLED": "1"}, service="ingestion"
    ).ok


def test_preflight_output_never_leaks_secret_values() -> None:
    # A differing REDIS_URL may embed a password; the reason must name the KEY, not the value.
    secret_url = "redis://:sup3rsecret@host:6379/0"
    result = verify_ownership_config_parity({**_GOOD_ENV, "REDIS_URL": secret_url}, dict(_GOOD_ENV))
    assert not result.ok
    assert all("sup3rsecret" not in r for r in result.reasons)


def test_preflight_key_sets_are_bounded_and_credential_free() -> None:
    for key in (*OWNERSHIP_PARITY_KEYS, *AUTHORITY_FLAG_KEYS):
        assert not any(s in key for s in ("PIN", "TOTP", "TOKEN", "SECRET", "PASSWORD"))


# =========================================================================== #
# §14/§15 — Gate I: explicit production ownership config + invariant
# =========================================================================== #
def test_env_example_declares_explicit_ownership_values() -> None:
    values = _env_example_values()
    assert values["MARKET_OWNERSHIP_ENABLED"] == "false"  # explicit + OFF by default
    assert values["MARKET_OWNERSHIP_LEASE_TTL_SECONDS"] == "30"
    assert values["MARKET_OWNERSHIP_RENEWAL_INTERVAL_SECONDS"] == "10"


def test_env_example_ownership_values_satisfy_the_invariant() -> None:
    values = _env_example_values()
    ttl = int(values["MARKET_OWNERSHIP_LEASE_TTL_SECONDS"])
    renewal = int(values["MARKET_OWNERSHIP_RENEWAL_INTERVAL_SECONDS"])
    assert 0 < renewal < ttl
    OwnershipLeaseConfig(lease_ttl_seconds=ttl, renewal_interval_seconds=renewal)  # constructs


def test_env_example_declares_authority_flags_off() -> None:
    values = _env_example_values()
    assert values["MARKET_OWNERSHIP_ENABLED"] == "false"
    assert values["IPC_AUTHORITATIVE_ENABLED"] == "false"
    assert values["MARKET_INGESTION_SERVICE_ENABLED"] == "false"


def test_ownership_lease_config_rejects_renewal_not_below_ttl() -> None:
    with pytest.raises(ValidationError):
        OwnershipLeaseConfig(lease_ttl_seconds=10, renewal_interval_seconds=30)


def test_settings_fail_fast_on_bad_ownership_timing_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError):
        _settings(
            monkeypatch,
            MARKET_OWNERSHIP_ENABLED="true",
            MARKET_OWNERSHIP_LEASE_TTL_SECONDS="10",
            MARKET_OWNERSHIP_RENEWAL_INTERVAL_SECONDS="30",
        )


# =========================================================================== #
# §16 — ownership Redis client is bounded by a transport-level socket timeout
# =========================================================================== #
def test_ownership_guard_client_has_bounded_socket_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        MARKET_OWNERSHIP_ENABLED="true",
        MARKET_OWNERSHIP_LEASE_TTL_SECONDS="30",
        MARKET_OWNERSHIP_RENEWAL_INTERVAL_SECONDS="10",
    )
    guard = build_provider_ownership_guard(settings, OwnerRole.BACKEND)
    assert guard is not None
    kwargs = guard._redis_to_close.connection_pool.connection_kwargs  # type: ignore[union-attr]
    assert kwargs.get("socket_timeout") == 10  # bounded to the renewal interval...
    assert kwargs["socket_timeout"] < settings.market_ownership_lease_ttl_seconds  # ...< the TTL


# =========================================================================== #
# §17/§23 — default/authority-OFF deployment is inert
# =========================================================================== #
def test_default_deployment_acquires_no_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)  # all defaults → ownership off
    assert settings.market_ownership_enabled is False
    assert build_provider_ownership_guard(settings, OwnerRole.BACKEND) is None
    assert build_provider_ownership_guard(settings, OwnerRole.INGESTION) is None


async def test_default_deployment_composes_an_inert_ingestion_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)  # market_ingestion_service_enabled defaults false
    service = await compose_market_ingestion_service(settings)
    assert service.enabled is False  # inert: no Dhan connect, no provider, no publication
    assert service.provider is None


def test_default_deployment_env_passes_authority_off_preflight() -> None:
    values = _env_example_values()
    assert verify_authority_off(values, service="template").ok


# =========================================================================== #
# Review LOW: the authority-off oracle matches pydantic's bool tokens and is
# fail-safe on unknown values (never false-passes an activation).
# =========================================================================== #
@pytest.mark.parametrize("token", ["1", "true", "t", "yes", "y", "on", "TRUE", "On", "maybe", "x"])
def test_authority_flag_on_for_truthy_and_unknown_tokens(token: str) -> None:
    # pydantic coerces true/t/yes/y/on/1 → True; an unrecognized value must NOT pass as OFF.
    assert _is_truthy(token) is True
    assert not verify_authority_off({"MARKET_OWNERSHIP_ENABLED": token}, service="s").ok


@pytest.mark.parametrize("token", ["0", "false", "f", "no", "n", "off", "", "FALSE"])
def test_authority_flag_off_only_for_explicit_false_tokens(token: str) -> None:
    assert _is_truthy(token) is False
    assert verify_authority_off({"MARKET_OWNERSHIP_ENABLED": token}, service="s").ok


def test_authority_flag_absent_is_off() -> None:
    assert _is_truthy(None) is False
    assert verify_authority_off({}, service="s").ok  # unset flag = default false = OFF


# =========================================================================== #
# Review MEDIUM: a deployed-but-inert container idles (not exit-loop) under
# unless-stopped; the direct/one-shot _run() still exits 0 immediately.
# =========================================================================== #
async def test_inert_entrypoint_idles_when_serving_as_a_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.market_ingestion import __main__ as entry

    async def _compose(_settings: object) -> MarketIngestionService:
        return MarketIngestionService(
            flags=PhaseHFlags(
                market_ingestion_service_enabled=False,
                ipc_publisher_enabled=False,
                ipc_consumer_enabled=False,
                ipc_shadow_compare_enabled=False,
                ipc_authoritative_enabled=False,
                legacy_market_path_enabled=True,
            )
        )

    idled = False

    async def _fake_idle() -> None:
        nonlocal idled
        idled = True

    monkeypatch.setattr(entry, "get_settings", lambda: object())
    monkeypatch.setattr(entry, "compose_market_ingestion_service", _compose)
    monkeypatch.setattr(entry, "_idle_until_signal", _fake_idle)

    assert await entry._run(serve_when_idle=True) == 0
    assert idled is True  # container mode: inert service idles until a signal (no exit-loop)

    idled = False
    assert await entry._run(serve_when_idle=False) == 0
    assert idled is False  # one-shot/test mode: inert service exits 0 at once
