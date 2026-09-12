"""Settings-level tests that the Phase-H flag matrix is wired and fails fast (DECOUPLING H1).

Confirms the default configuration derives LEGACY_ONLY (production behaviour unchanged) and that
an illegal flag combination is rejected at Settings construction. Uses ``_env_file=None`` so a
developer's local ``.env`` cannot influence the result.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.market_ingestion.mode import MarketPathMode

_REQUIRED = {
    "DATABASE_URL": "postgresql+asyncpg://apexscan:pw@postgres:5432/apexscan",
    "REDIS_URL": "redis://redis:6379/0",
}


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    for key, value in {**_REQUIRED, **overrides}.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_default_settings_derive_legacy_only(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    assert settings.market_path_mode() is MarketPathMode.LEGACY_ONLY
    assert settings.legacy_market_path_enabled is True
    assert settings.market_ingestion_service_enabled is False


def test_settings_reject_dual_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _settings(
            monkeypatch,
            IPC_CONSUMER_ENABLED="true",
            IPC_AUTHORITATIVE_ENABLED="true",  # legacy defaults true → dual authority
        )
    assert "mutually exclusive" in str(excinfo.value)


def test_settings_reject_consumer_without_role(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _settings(monkeypatch, IPC_CONSUMER_ENABLED="true")
    assert "poisoning dedup" in str(excinfo.value)


def test_settings_market_ipc_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)
    config = settings.market_ipc_config()
    assert config.enabled is False  # inert until composed
    assert config.stream_name == "md:events"
