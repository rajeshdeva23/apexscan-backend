"""Application settings.

Centralised, type-safe configuration loaded from environment variables via
Pydantic Settings. This is the single source of truth for runtime config —
no module reads ``os.environ`` directly, and there are no global mutable
config objects. Settings are provided to the app through dependency
injection (see :func:`app.core.config.get_settings`).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    # Application-level stale-live-feed watchdog (DEPLOY-8.5; two-threshold since DEPLOY-9.6).
    # Session-gated, monotonic-clock, measured since the last VALID canonical market event.
    # SOFT threshold: after this many seconds with no canonical event the feed is logged once
    # as *suspected stale* (degraded observability only — NO reconnect). This tolerates the
    # legitimate low-tick lulls seen at session boundaries (open/close) without churn.
    dhan_live_stale_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    # HARD threshold: only after this many seconds with no canonical event does the adapter
    # treat the feed as genuinely stuck and perform one bounded reconnect (reusing the
    # existing reconnect policy). Must be strictly greater than the soft threshold. This is
    # the stuck-feed detection latency ceiling.
    dhan_live_hard_stale_timeout_seconds: float = Field(default=120.0, gt=0, le=600)

    # --- Market provider runtime (ADR-010 D14) -----------------------------
    # Explicit switch: the live-market runtime (provider + universe + ingestion)
    # is composed only when this is true. Never inferred from credential presence,
    # environment name, or debug. Default off so ordinary local/CI runs stay dormant.
    market_provider_enabled: bool = Field(default=False)
    # Enables the read-only in-process Session-OHLC evidence observer (ADR-015; DEPLOY-10 R4D).
    # Evidence collection ONLY — never an authority or strategy flag. Default off: when false
    # the observer is not constructed, so there is zero subscription, REST, artifact, or
    # behavioral difference.
    session_ohlc_evidence_observer_enabled: bool = Field(default=False)
    # Running image tag, injected at deploy for evidence provenance (ADR-015 D10); None when the
    # runtime cannot derive it (recorded as unknown, never fabricated).
    apexscan_image_tag: str | None = Field(default=None)
    # Consecutive-failure threshold moving a strategy to ERROR (docs/07 §20 leaves the
    # number to configuration). Exercised only once concrete strategies run.
    strategy_error_threshold: int = Field(default=3, ge=1)
    # Comma-separated strategy ids to enable in production (ADR-013 REG3). Default empty:
    # zero production strategies started. Only catalog-known ids resolve; the composition
    # fails closed on an unknown id. This is the sole strategy-enablement seam — no
    # strategy-specific fields live in Settings (strategy config lives in the catalog).
    strategies_enabled: str = Field(default="")
    # Infrastructure wake interval for the session-statistics refresh driver — how often it
    # evaluates the phase/demand/cadence gate (ADR-009 addendum). This is NOT the refresh
    # cadence (the coordinator owns that via the strictest freshness max_age); keep it at or
    # below the strictest configured max_age so no due transition is missed.
    session_statistics_refresh_poll_seconds: float = Field(default=1.0, gt=0, le=60)

    # --- Market session (NSE cash-equity; ADR-004) -------------------------
    # Exchange timezone for interpreting canonical UTC timestamps into the
    # trading date and session phase; canonical timestamps stay UTC.
    exchange_timezone: str = Field(default="Asia/Kolkata")
    # Exchange-local HH:MM session boundaries (configuration, not embedded
    # assumptions — docs/06 §8); defaults reflect the NSE regular schedule.
    nse_pre_open_start: str = Field(default="09:00")
    nse_opening_auction_start: str = Field(default="09:08")
    nse_regular_open: str = Field(default="09:15")
    nse_regular_close: str = Field(default="15:30")
    nse_closing_end: str = Field(default="15:40")
    # Comma-separated ISO trading-holiday dates (deterministic; no remote fetch).
    nse_holidays: str = Field(default="")

    # --- Secondary calendar monitor (ADR-011; observation-only) ------------
    # Off by default and never inferred: when enabled, a runtime-owned task fetches a
    # public, unauthenticated secondary calendar page once per exchange-local day and
    # compares it against the authoritative dataset. It never mutates any calendar
    # authority; a discrepancy is a review signal only.
    calendar_monitor_enabled: bool = Field(default=False)
    # Exchange-local HH:MM at/after which the daily check fires (before the 09:15 open).
    calendar_monitor_run_time: str = Field(default="08:00")
    # Bounded per-request timeout for the secondary page fetch.
    calendar_monitor_request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

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

    @field_validator("exchange_timezone")
    @classmethod
    def validate_exchange_timezone(cls, value: str) -> str:
        """Require a resolvable IANA timezone for exchange-session interpretation."""
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("EXCHANGE_TIMEZONE must be a valid IANA timezone name") from error
        return normalized

    @field_validator(
        "nse_pre_open_start",
        "nse_opening_auction_start",
        "nse_regular_open",
        "nse_regular_close",
        "nse_closing_end",
        "calendar_monitor_run_time",
    )
    @classmethod
    def validate_session_time(cls, value: str) -> str:
        """Require each session boundary to be an exchange-local ``HH:MM`` time."""
        normalized = value.strip()
        try:
            datetime.strptime(normalized, "%H:%M").replace(tzinfo=UTC)
        except ValueError as error:
            raise ValueError("session boundary times must be formatted as HH:MM") from error
        return normalized

    @field_validator("nse_holidays")
    @classmethod
    def validate_holidays(cls, value: str) -> str:
        """Require every configured holiday to be an ISO (YYYY-MM-DD) date."""
        for entry in value.split(","):
            candidate = entry.strip()
            if not candidate:
                continue
            try:
                date.fromisoformat(candidate)
            except ValueError as error:
                raise ValueError("NSE_HOLIDAYS entries must be ISO dates (YYYY-MM-DD)") from error
        return value

    @field_validator("strategies_enabled")
    @classmethod
    def validate_strategies_enabled(cls, value: str) -> str:
        """Require each enabled entry to be a lowercase snake_case strategy id (fail safe)."""
        for entry in value.split(","):
            candidate = entry.strip()
            if candidate and re.fullmatch(r"[a-z][a-z0-9_]*", candidate) is None:
                raise ValueError(
                    "STRATEGIES_ENABLED entries must be lowercase snake_case strategy ids"
                )
        return value

    @model_validator(mode="after")
    def validate_stale_watchdog_thresholds(self) -> Self:
        """Require the hard reconnect threshold to exceed the soft suspect threshold.

        The two-threshold watchdog (DEPLOY-9.6) only makes sense when the reconnect
        (hard) deadline is strictly larger than the suspect (soft) deadline; an inverted
        or equal relationship would collapse back to single-threshold churn.
        """
        if self.dhan_live_hard_stale_timeout_seconds <= self.dhan_live_stale_timeout_seconds:
            raise ValueError(
                "DHAN_LIVE_HARD_STALE_TIMEOUT_SECONDS must be greater than "
                "DHAN_LIVE_STALE_TIMEOUT_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def validate_provider_enabled_credentials(self) -> Self:
        """Require the auth-mode credentials when the market provider runtime is enabled.

        Fail fast at configuration time (ADR-010 D14): an enabled provider must never
        start with missing credentials, and enablement is never inferred.
        """
        if not self.market_provider_enabled:
            return self
        if self.dhan_auth_mode == "access_token":
            if not _has_secret_value(self.dhan_access_token):
                raise ValueError(
                    "DHAN_ACCESS_TOKEN must be configured when MARKET_PROVIDER_ENABLED=true "
                    "and DHAN_AUTH_MODE=access_token"
                )
            return self
        for setting_name, secret in (
            ("DHAN_CLIENT_ID", self.dhan_client_id),
            ("DHAN_PIN", self.dhan_pin),
            ("DHAN_TOTP_SECRET", self.dhan_totp_secret),
        ):
            if not _has_secret_value(secret):
                raise ValueError(
                    f"{setting_name} must be configured when MARKET_PROVIDER_ENABLED=true "
                    "and DHAN_AUTH_MODE=totp"
                )
        return self

    # Pre-existing Phase-2/3 complexity (11 > 8); tracked debt. Refactor is out
    # of P4.0 scope; new code is gated by C901 (docs/11 Rule 16).
    @model_validator(mode="after")
    def validate_production_safety(self) -> Self:  # noqa: C901
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
    def strategies_enabled_list(self) -> list[str]:
        """Return the enabled strategy ids as a clean list of non-empty strings."""
        return [entry.strip() for entry in self.strategies_enabled.split(",") if entry.strip()]

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
