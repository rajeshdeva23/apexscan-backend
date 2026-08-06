"""Behavioural tests for centralized runtime configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import ConfigurationError, Settings, get_settings

_CONFIGURATION_KEYS = (
    "APP_NAME",
    "APP_ENV",
    "APP_DEBUG",
    "APP_VERSION",
    "API_V1_PREFIX",
    "LOG_LEVEL",
    "BACKEND_HOST",
    "BACKEND_PORT",
    "CORS_ORIGINS",
    "DATABASE_URL",
    "REDIS_URL",
    "PROVIDER_LIFECYCLE_TIMEOUT_SECONDS",
    "DHAN_AUTH_MODE",
    "DHAN_CLIENT_ID",
    "DHAN_PIN",
    "DHAN_TOTP_SECRET",
    "DHAN_ACCESS_TOKEN",
    "DHAN_REST_BASE_URL",
    "DHAN_REST_TIMEOUT_SECONDS",
    "DHAN_LIVE_SMOKE_ENABLED",
)

_VALID_REQUIRED_VALUES = {
    "DATABASE_URL": "postgresql+asyncpg://apexscan:config-test-password@postgres:5432/apexscan",
    "REDIS_URL": "redis://redis:6379/0",
}


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    """Prevent one test's process configuration from leaking into another."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _set_environment(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    """Replace all recognized configuration environment variables for one test."""
    for key in _CONFIGURATION_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _write_dotenv(path: Path, values: dict[str, str]) -> Path:
    """Write a controlled dotenv fixture without relying on local developer state."""
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    return path


def test_loads_valid_configuration_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A complete valid environment produces typed settings."""
    _set_environment(
        monkeypatch,
        {
            **_VALID_REQUIRED_VALUES,
            "APP_ENV": "staging",
            "APP_DEBUG": "false",
            "BACKEND_PORT": "8100",
            "CORS_ORIGINS": "https://console.example.com",
        },
    )

    settings = Settings(_env_file=None)

    assert settings.app_env == "staging"
    assert settings.app_debug is False
    assert settings.backend_port == 8100
    assert settings.cors_origins_list == ["https://console.example.com"]


def test_rejects_missing_required_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Booting without DATABASE_URL fails with an actionable variable name."""
    _set_environment(monkeypatch, {"REDIS_URL": _VALID_REQUIRED_VALUES["REDIS_URL"]})

    with pytest.raises(ConfigurationError) as captured:
        get_settings()

    assert "DATABASE_URL" in str(captured.value)


def test_rejects_malformed_port_without_echoing_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed typed value aborts loading without echoing its input value."""
    _set_environment(
        monkeypatch,
        {**_VALID_REQUIRED_VALUES, "BACKEND_PORT": "not-a-port"},
    )

    with pytest.raises(ConfigurationError) as captured:
        get_settings()

    diagnostic = str(captured.value)
    assert "BACKEND_PORT" in diagnostic
    assert "not-a-port" not in diagnostic


def test_process_environment_overrides_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deployment-time process variable wins over the dotenv value."""
    _set_environment(monkeypatch, {"APP_NAME": "process-name"})
    dotenv = _write_dotenv(
        tmp_path / ".env",
        {**_VALID_REQUIRED_VALUES, "APP_NAME": "dotenv-name"},
    )

    settings = Settings(_env_file=dotenv)

    assert settings.app_name == "process-name"


def test_dotenv_overrides_optional_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A local dotenv value wins over an optional in-code default."""
    _set_environment(monkeypatch, {})
    dotenv = _write_dotenv(
        tmp_path / ".env",
        {**_VALID_REQUIRED_VALUES, "APP_NAME": "dotenv-name"},
    )

    settings = Settings(_env_file=dotenv)

    assert settings.app_name == "dotenv-name"


def test_optional_defaults_remain_available_for_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local development needs only its required service connection values."""
    _set_environment(monkeypatch, _VALID_REQUIRED_VALUES)

    settings = Settings(_env_file=None)

    assert settings.app_name == "ApexScan"
    assert settings.app_env == "development"
    assert settings.app_debug is True
    assert settings.backend_port == 8000
    assert settings.provider_lifecycle_timeout_seconds == 5.0


def test_dhan_settings_are_optional_and_safe_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary local and CI configuration needs no Dhan credential."""
    _set_environment(monkeypatch, _VALID_REQUIRED_VALUES)

    settings = Settings(_env_file=None)

    assert settings.dhan_auth_mode == "totp"
    assert settings.dhan_client_id is None
    assert settings.dhan_pin is None
    assert settings.dhan_totp_secret is None
    assert settings.dhan_access_token is None
    assert settings.dhan_rest_base_url == "https://api.dhan.co/v2"
    assert settings.dhan_rest_timeout_seconds == 10.0
    assert settings.dhan_live_smoke_enabled is False


def test_inactive_dhan_example_placeholders_do_not_block_ordinary_local_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copying the optional example values must not make non-Dhan development unusable."""
    _set_environment(
        monkeypatch,
        {
            **_VALID_REQUIRED_VALUES,
            "DHAN_AUTH_MODE": "totp",
            "DHAN_CLIENT_ID": "replace_with_secure_dhan_client_id",
            "DHAN_PIN": "replace_with_secure_six_digit_dhan_pin",
            "DHAN_TOTP_SECRET": "replace_with_secure_dhan_totp_secret",
        },
    )

    settings = Settings(_env_file=None)

    assert settings.dhan_live_smoke_enabled is False
    assert settings.dhan_auth_mode == "totp"


@pytest.mark.parametrize(
    ("missing_key", "configured_credentials"),
    [
        (
            "DHAN_CLIENT_ID",
            {
                "DHAN_PIN": "123456",
                "DHAN_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
            },
        ),
        (
            "DHAN_PIN",
            {
                "DHAN_CLIENT_ID": "fixture-client-id",
                "DHAN_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
            },
        ),
        (
            "DHAN_TOTP_SECRET",
            {
                "DHAN_CLIENT_ID": "fixture-client-id",
                "DHAN_PIN": "123456",
            },
        ),
    ],
)
def test_dhan_live_smoke_totp_mode_requires_each_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
    missing_key: str,
    configured_credentials: dict[str, str],
) -> None:
    """A missing TOTP credential must fail preflight before any live request can occur."""
    _set_environment(
        monkeypatch,
        {
            **_VALID_REQUIRED_VALUES,
            "DHAN_LIVE_SMOKE_ENABLED": "true",
            **configured_credentials,
        },
    )

    with pytest.raises(ConfigurationError) as captured:
        get_settings()

    assert missing_key in str(captured.value)


def test_dhan_live_smoke_manual_mode_requires_a_configured_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicitly selected manual mode cannot continue with a blank token."""
    _set_environment(
        monkeypatch,
        {
            **_VALID_REQUIRED_VALUES,
            "DHAN_LIVE_SMOKE_ENABLED": "true",
            "DHAN_AUTH_MODE": "access_token",
            "DHAN_ACCESS_TOKEN": "   ",
        },
    )

    with pytest.raises(ConfigurationError) as captured:
        get_settings()

    assert "DHAN_ACCESS_TOKEN" in str(captured.value)


def test_dhan_live_smoke_manual_mode_does_not_require_totp_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual mode must be an explicit, unambiguous developer-only alternative."""
    _set_environment(
        monkeypatch,
        {
            **_VALID_REQUIRED_VALUES,
            "DHAN_LIVE_SMOKE_ENABLED": "true",
            "DHAN_AUTH_MODE": "access_token",
            "DHAN_ACCESS_TOKEN": "fixture-manual-access-token",
        },
    )

    settings = Settings(_env_file=None)

    assert settings.dhan_auth_mode == "access_token"
    assert settings.dhan_client_id is None
    assert settings.dhan_totp_secret is None


def test_dhan_access_token_is_redacted_from_settings_representation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured Dhan access token never appears in printable settings diagnostics."""
    secret = "test-dhan-access-token-must-not-leak"
    _set_environment(
        monkeypatch,
        {**_VALID_REQUIRED_VALUES, "DHAN_ACCESS_TOKEN": secret},
    )

    settings = Settings(_env_file=None)

    assert secret not in repr(settings)
    assert secret not in str(settings.dhan_access_token)
    assert secret not in str(settings.model_dump())


def test_dhan_totp_credentials_are_redacted_from_settings_and_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing Dhan diagnostics to include any TOTP credential must fail this boundary."""
    client_id = "fixture-client-id-must-not-leak"
    pin = "654321"
    secret = "JBSWY3DPEHPK3PXP"
    _set_environment(
        monkeypatch,
        {
            **_VALID_REQUIRED_VALUES,
            "DHAN_LIVE_SMOKE_ENABLED": "true",
            "DHAN_CLIENT_ID": client_id,
            "DHAN_PIN": pin,
            "DHAN_TOTP_SECRET": secret,
            "DHAN_REST_BASE_URL": "http://invalid.example",
        },
    )

    with pytest.raises(ConfigurationError) as captured:
        get_settings()

    diagnostic = str(captured.value)
    assert "DHAN_REST_BASE_URL" in diagnostic
    assert client_id not in diagnostic
    assert pin not in diagnostic
    assert secret not in diagnostic


def test_rejects_insecure_dhan_rest_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dhan credentials are never configured for a non-TLS REST endpoint."""
    _set_environment(
        monkeypatch,
        {**_VALID_REQUIRED_VALUES, "DHAN_REST_BASE_URL": "http://api.dhan.co/v2"},
    )

    with pytest.raises(ConfigurationError) as captured:
        get_settings()

    assert "DHAN_REST_BASE_URL" in str(captured.value)


def test_rejects_nonpositive_provider_lifecycle_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider lifecycle operations must have a positive bounded timeout."""
    _set_environment(
        monkeypatch,
        {**_VALID_REQUIRED_VALUES, "PROVIDER_LIFECYCLE_TIMEOUT_SECONDS": "0"},
    )

    with pytest.raises(ConfigurationError) as captured:
        get_settings()

    assert "PROVIDER_LIFECYCLE_TIMEOUT_SECONDS" in str(captured.value)


def test_rejects_debug_mode_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production configuration cannot enable FastAPI debug behaviour."""
    _set_environment(
        monkeypatch,
        {
            **_VALID_REQUIRED_VALUES,
            "APP_ENV": "production",
            "APP_DEBUG": "true",
            "CORS_ORIGINS": "https://console.example.com",
        },
    )

    with pytest.raises(ConfigurationError) as captured:
        get_settings()

    assert "APP_DEBUG" in str(captured.value)
    assert "production" in str(captured.value)


def test_redacts_secret_bearing_url_from_validation_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid connection URLs identify the variable without revealing credentials."""
    secret = "must-not-appear-in-errors"
    _set_environment(
        monkeypatch,
        {
            "DATABASE_URL": f"mysql://apexscan:{secret}@database:3306/apexscan",
            "REDIS_URL": _VALID_REQUIRED_VALUES["REDIS_URL"],
        },
    )

    with pytest.raises(ConfigurationError) as captured:
        get_settings()

    diagnostic = str(captured.value)
    assert "DATABASE_URL" in diagnostic
    assert "postgresql+asyncpg" in diagnostic
    assert secret not in diagnostic


def test_repeated_loading_is_cached_and_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """The centralized provider parses one stable settings instance per process."""
    _set_environment(monkeypatch, _VALID_REQUIRED_VALUES)

    first = get_settings()
    second = get_settings()

    assert first is second
    assert first.model_dump() == second.model_dump()
