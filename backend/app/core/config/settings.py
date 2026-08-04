"""Application settings.

Centralised, type-safe configuration loaded from environment variables via
Pydantic Settings. This is the single source of truth for runtime config —
no module reads ``os.environ`` directly, and there are no global mutable
config objects. Settings are provided to the app through dependency
injection (see :func:`app.core.config.get_settings`).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ALLOWED_ENVIRONMENTS = frozenset({"development", "staging", "production"})
_ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_LOCAL_CORS_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


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

    @model_validator(mode="after")
    def validate_production_safety(self) -> Self:
        """Reject settings that are safe only for local development in production."""
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
