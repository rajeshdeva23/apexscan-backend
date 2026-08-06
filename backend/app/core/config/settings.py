"""Application settings.

Centralised, type-safe configuration loaded from environment variables via
Pydantic Settings. This is the single source of truth for runtime config —
no module reads ``os.environ`` directly, and there are no global mutable
config objects. Settings are provided to the app through dependency
injection (see :func:`app.core.config.get_settings`).
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ALLOWED_ENVIRONMENTS = frozenset({"development", "staging", "production"})
_ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_LOCAL_CORS_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_DHAN_AUTH_MODES = frozenset({"totp", "access_token"})


class ConfigurationError(RuntimeError):
    """A safe, actionable startup configuration error."""


class Settings(BaseSettings):
    """Strongly-typed application configuration.

    Values are resolved in deterministic order: process environment, local
    ``.env`` file, then the optional in-code defaults below. Field names map
    to upper-case environment keys automatically.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
        validate_default=True,
    )

    # --- Application -------------------------------------------------------
    app_name: str = Field(default="ApexScan")
    app_env: str = Field(default="development")
    app_debug: bool = Field(default=True)
    app_version: str = Field(default="0.1.0")
    api_v1_prefix: str = Field(default="/api/v1")
    log_level: str = Field(default="INFO")

    # --- Server ------------------------------------------------------------
    backend_host: str = Field(default="0.0.0.0")
    backend_port: int = Field(default=8000)

    # --- CORS --------------------------------------------------------------
    # Comma-separated origins string; parsed into a list by ``cors_origins``.
    cors_origins: str = Field(default="http://localhost:5173")

    # --- PostgreSQL --------------------------------------------------------
    database_url: str = Field(..., repr=False)

    # --- Redis -------------------------------------------------------------
    redis_url: str = Field(..., repr=False)

    # --- Provider lifecycle ------------------------------------------------
    provider_lifecycle_timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    # --- Dhan REST adapter -------------------------------------------------
    # The adapter is not application-wired in P3.3, so Dhan credentials remain
    # optional except for explicitly enabled protected live-smoke validation.
    dhan_auth_mode: str = Field(default="totp")
    dhan_client_id: SecretStr | None = Field(default=None, repr=False)
    dhan_pin: SecretStr | None = Field(default=None, repr=False)
    dhan_totp_secret: SecretStr | None = Field(default=None, repr=False)
    dhan_access_token: SecretStr | None = Field(default=None, repr=False)
    dhan_rest_base_url: str = Field(default="https://api.dhan.co/v2")
    dhan_rest_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    dhan_live_smoke_enabled: bool = Field(default=False)

    @field_validator("app_env")
    @classmethod
    def normalize_app_env(cls, value: str) -> str:
        """Normalize and restrict the declared deployment environment."""
        normalized = value.strip().lower()
        if normalized not in _ALLOWED_ENVIRONMENTS:
            allowed = ", ".join(sorted(_ALLOWED_ENVIRONMENTS))
            raise ValueError(f"APP_ENV must be one of: {allowed}")
        return normalized

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        """Normalize and restrict logging levels accepted by the application."""
        normalized = value.strip().upper()
        if normalized not in _ALLOWED_LOG_LEVELS:
            allowed = ", ".join(sorted(_ALLOWED_LOG_LEVELS))
            raise ValueError(f"LOG_LEVEL must be one of: {allowed}")
        return normalized

    @field_validator("dhan_auth_mode")
    @classmethod
    def normalize_dhan_auth_mode(cls, value: str) -> str:
        """Require a single explicit Dhan authentication path when one is used."""
        normalized = value.strip().lower()
        if normalized not in _DHAN_AUTH_MODES:
            allowed = ", ".join(sorted(_DHAN_AUTH_MODES))
            raise ValueError(f"DHAN_AUTH_MODE must be one of: {allowed}")
        return normalized

    @field_validator("backend_port")
    @classmethod
    def validate_backend_port(cls, value: int) -> int:
        """Require a usable TCP port number."""
        if not 1 <= value <= 65535:
            raise ValueError("BACKEND_PORT must be between 1 and 65535")
        return value

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, value: str) -> str:
        """Require an explicit comma-separated list of HTTP(S) origins."""
        origins = [origin.strip() for origin in value.split(",") if origin.strip()]
        if not origins:
            raise ValueError("CORS_ORIGINS must contain at least one HTTP(S) origin")

        for origin in origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("CORS_ORIGINS entries must be HTTP(S) origins without paths")

        return ",".join(origins)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        """Require the configured async PostgreSQL connection scheme and host."""
        parsed = urlsplit(value)
        if parsed.scheme != "postgresql+asyncpg" or not parsed.hostname:
            raise ValueError(
                "DATABASE_URL must use the postgresql+asyncpg scheme and include a host"
            )
        return value

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: str) -> str:
        """Require a Redis connection URL with an explicit host."""
        parsed = urlsplit(value)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError("REDIS_URL must use the redis or rediss scheme and include a host")
        return value

    @field_validator("dhan_rest_base_url")
    @classmethod
    def validate_dhan_rest_base_url(cls, value: str) -> str:
        """Restrict Dhan REST traffic to an explicit HTTPS API base URL."""
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
            raise ValueError("DHAN_REST_BASE_URL must be an HTTPS URL without query or fragment")
        return normalized

    @model_validator(mode="after")
    def validate_production_safety(self) -> Self:
        """Reject settings that are safe only for local development in production."""
        if self.dhan_live_smoke_enabled:
            if self.dhan_auth_mode == "access_token":
                if not _has_secret_value(self.dhan_access_token):
                    raise ValueError(
                        "DHAN_ACCESS_TOKEN must be configured when "
                        "DHAN_AUTH_MODE=access_token and DHAN_LIVE_SMOKE_ENABLED=true"
                    )
            else:
                required_totp_settings = (
                    ("DHAN_CLIENT_ID", self.dhan_client_id),
                    ("DHAN_PIN", self.dhan_pin),
                    ("DHAN_TOTP_SECRET", self.dhan_totp_secret),
                )
                for setting_name, secret in required_totp_settings:
                    if not _has_secret_value(secret):
                        raise ValueError(
                            f"{setting_name} must be configured when DHAN_AUTH_MODE=totp "
                            "and DHAN_LIVE_SMOKE_ENABLED=true"
                        )
                if (
                    self.dhan_pin is not None
                    and re.fullmatch(r"[0-9]{6}", self.dhan_pin.get_secret_value().strip()) is None
                ):
                    raise ValueError("DHAN_PIN must be a six-digit numeric code")

        if not self.is_production:
            return self

        if self.app_debug:
            raise ValueError("APP_DEBUG must be false when APP_ENV=production")

        for origin in self.cors_origins_list:
            parsed = urlsplit(origin)
            if parsed.scheme != "https" or parsed.hostname in _LOCAL_CORS_HOSTS:
                raise ValueError(
                    "CORS_ORIGINS must contain explicit HTTPS non-local origins when "
                    "APP_ENV=production"
                )

        return self

    @property
    def cors_origins_list(self) -> list[str]:
        """Return CORS origins as a clean list of non-empty strings."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        """True when running in the production environment."""
        return self.app_env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance.

    Cached so configuration is parsed once per process. Use this as a FastAPI
    dependency rather than instantiating ``Settings`` directly.
    """
    try:
        return Settings()
    except ValidationError as error:
        raise ConfigurationError(_format_validation_error(error)) from None


def _format_validation_error(error: ValidationError) -> str:
    """Render validation diagnostics without including source configuration values."""
    diagnostics = []
    for detail in error.errors(include_input=False, include_url=False):
        location = detail["loc"]
        field_name = ".".join(str(part).upper() for part in location) or "CONFIGURATION"
        diagnostics.append(f"{field_name}: {detail['msg']}")

    return "Configuration validation failed. Correct the following: " + "; ".join(diagnostics)


def _has_secret_value(value: SecretStr | None) -> bool:
    """Return whether a secret setting contains non-whitespace runtime material."""
    return value is not None and bool(value.get_secret_value().strip())
