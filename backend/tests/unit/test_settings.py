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
