"""Application settings.

Centralised, type-safe configuration loaded from environment variables via
Pydantic Settings. This is the single source of truth for runtime config —
no module reads ``os.environ`` directly, and there are no global mutable
config objects. Settings are provided to the app through dependency
injection (see :func:`app.core.config.get_settings`).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application configuration.

    Values are read from environment variables (and a local ``.env`` file in
    development). Field names map to upper-case env keys automatically.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
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
    database_url: str = Field(
        default="postgresql+asyncpg://apexscan:change_me_in_local_env@localhost:5432/apexscan"
    )

    # --- Redis -------------------------------------------------------------
    redis_url: str = Field(default="redis://localhost:6379/0")

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
    return Settings()
