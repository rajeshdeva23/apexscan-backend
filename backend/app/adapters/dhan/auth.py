"""Dhan-local runtime authentication using its documented TOTP token endpoint."""

from __future__ import annotations

import asyncio
import binascii
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx
import pyotp
from pydantic import SecretStr

from app.adapters.base.errors import (
    NormalizationError,
    ProviderAuthenticationError,
    ProviderNetworkError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.core.config import Settings

_AUTH_BASE_URL = "https://auth.dhan.co"
_GENERATE_ACCESS_TOKEN_PATH = "/app/generateAccessToken"
_INDIAN_STANDARD_TIME = ZoneInfo("Asia/Kolkata")
_REGENERATION_WINDOW = timedelta(minutes=5)
_AUTHENTICATION_ERROR_CODES = frozenset({"DH-901", "DH-902"})
_RATE_LIMIT_ERROR_CODES = frozenset({"DH-904"})
_UNAVAILABLE_ERROR_CODES = frozenset({"DH-903", "DH-908", "DH-909"})


class _DhanAuthLogRedactor(logging.Filter):
    """Redact the only HTTPX request URL that carries Dhan credentials in its query."""

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            _GENERATE_ACCESS_TOKEN_PATH in record.getMessage()
            and "auth.dhan.co" in record.getMessage()
        ):
            record.msg = "HTTP Request: Dhan access-token generation (query redacted)"
            record.args = ()
        return True


_DHAN_AUTH_LOG_REDACTOR = _DhanAuthLogRedactor()


class DhanAccessTokenProvider(Protocol):
    """Runtime-only source of a Dhan REST access token."""

    async def get_access_token(self) -> SecretStr:
        """Return a currently usable access token without exposing it in diagnostics."""

    async def disconnect(self) -> None:
        """Discard runtime authentication material and close owned resources."""


class DhanStaticAccessTokenProvider:
    """Explicit manual-token source retained only for selected developer troubleshooting mode."""

    def __init__(self, access_token: SecretStr) -> None:
        token = access_token.get_secret_value().strip()
        if not token:
            raise ProviderAuthenticationError()
        self._access_token = SecretStr(token)

    async def get_access_token(self) -> SecretStr:
        """Return the caller-supplied runtime token without any network I/O."""
        return self._access_token

    async def disconnect(self) -> None:
        """Discard the manual runtime token when its owning adapter disconnects."""
        self._access_token = SecretStr("")


@dataclass(frozen=True, slots=True)
class _RuntimeAccessToken:
    """Private, in-memory Dhan token state; never a canonical provider contract."""

    access_token: SecretStr = field(repr=False)
    expires_at: datetime

    def is_usable(self, now: datetime) -> bool:
        """Return whether the token has room to complete an imminent Dhan request."""
        return now + _REGENERATION_WINDOW < self.expires_at


class DhanAuthManager:
    """Obtain and cache Dhan TOTP-generated access tokens only in process memory."""

    def __init__(
        self,
        *,
        client_id: SecretStr,
        pin: SecretStr,
        totp_secret: SecretStr,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client_id = _required_secret(client_id)
        self._pin = _required_secret(pin)
        self._totp_secret = _required_secret(totp_secret)
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._clock = clock
        logging.getLogger("httpx").addFilter(_DHAN_AUTH_LOG_REDACTOR)
        self._client: httpx.AsyncClient | None = None
        self._runtime_token: _RuntimeAccessToken | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> DhanAuthManager:
        """Create TOTP authentication from centralized settings without widening its scope."""
        if (
            settings.dhan_client_id is None
            or settings.dhan_pin is None
            or settings.dhan_totp_secret is None
        ):
            raise ProviderAuthenticationError()
        return cls(
            client_id=settings.dhan_client_id,
            pin=settings.dhan_pin,
            totp_secret=settings.dhan_totp_secret,
            timeout_seconds=settings.dhan_rest_timeout_seconds,
            transport=transport,
        )

    @property
    def current_token_expires_at(self) -> datetime | None:
        """Expose only the parsed expiry for safe live-smoke reporting."""
        if self._runtime_token is None:
            return None
        return self._runtime_token.expires_at

    async def get_access_token(self) -> SecretStr:
        """Return a cached usable token or make one documented, bounded auth request."""
        async with self._lock:
            now = _as_utc(self._clock())
            if self._runtime_token is not None and self._runtime_token.is_usable(now):
                return self._runtime_token.access_token

            totp = self._generate_totp(now)
            client = self._require_client()
            try:
                response = await client.post(
                    _GENERATE_ACCESS_TOKEN_PATH,
                    params={
                        "dhanClientId": self._client_id.get_secret_value(),
                        "pin": self._pin.get_secret_value(),
                        "totp": totp.get_secret_value(),
                    },
                )
            except httpx.TimeoutException:
                raise ProviderTimeoutError() from None
            except httpx.RequestError:
                raise ProviderNetworkError() from None

            self._raise_for_auth_failure(response)
            self._runtime_token = _parse_runtime_token(response, now)
            return self._runtime_token.access_token

    async def disconnect(self) -> None:
        """Discard ephemeral auth state and close the Dhan auth client idempotently."""
        self._runtime_token = None
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    def _generate_totp(self, now: datetime) -> SecretStr:
        """Generate one transient six-digit code using the standard RFC 6238 implementation."""
        try:
            value = pyotp.TOTP(self._totp_secret.get_secret_value()).at(now)
        except (binascii.Error, TypeError, ValueError):
            raise ProviderAuthenticationError() from None
        if len(value) != 6 or not value.isdecimal():
            raise ProviderAuthenticationError()
        return SecretStr(value)

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=_AUTH_BASE_URL,
                timeout=httpx.Timeout(self._timeout_seconds),
                transport=self._transport,
            )
        return self._client

    @staticmethod
    def _raise_for_auth_failure(response: httpx.Response) -> None:
        if response.is_success:
            return

        error_code = _error_code(response)
        if response.status_code == 429 or error_code in _RATE_LIMIT_ERROR_CODES:
            raise ProviderRateLimitError()
        if response.status_code == 408:
            raise ProviderTimeoutError()
        if response.status_code >= 500 or error_code in _UNAVAILABLE_ERROR_CODES:
            raise ProviderUnavailableError()
        if response.status_code in {401, 403} or error_code in _AUTHENTICATION_ERROR_CODES:
            raise ProviderAuthenticationError()
        raise ProviderAuthenticationError()


def _required_secret(value: SecretStr) -> SecretStr:
    """Normalize a non-empty secret while preserving Pydantic's redacted representation."""
    normalized = value.get_secret_value().strip()
    if not normalized:
        raise ProviderAuthenticationError()
    return SecretStr(normalized)


def _as_utc(value: datetime) -> datetime:
    """Normalize an injected clock value for deterministic token expiry decisions."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_runtime_token(response: httpx.Response, now: datetime) -> _RuntimeAccessToken:
    """Accept only the two documented token fields and ignore personal response data."""
    try:
        payload: object = response.json()
    except (TypeError, ValueError):
        raise NormalizationError() from None
    if not isinstance(payload, Mapping):
        raise NormalizationError()

    raw_token = payload.get("accessToken")
    raw_expiry = payload.get("expiryTime")
    if not isinstance(raw_token, str) or not raw_token.strip():
        raise NormalizationError()
    if not isinstance(raw_expiry, str) or not raw_expiry.strip():
        raise NormalizationError()

    try:
        parsed_expiry = datetime.fromisoformat(raw_expiry.strip())
    except ValueError:
        raise NormalizationError() from None
    if parsed_expiry.tzinfo is None:
        parsed_expiry = parsed_expiry.replace(tzinfo=_INDIAN_STANDARD_TIME)
    expires_at = parsed_expiry.astimezone(UTC)
    if expires_at <= now:
        raise NormalizationError()
    return _RuntimeAccessToken(access_token=SecretStr(raw_token.strip()), expires_at=expires_at)


def _error_code(response: httpx.Response) -> str | None:
    """Read only Dhan's safe error classifier, never provider error text."""
    try:
        payload: object = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("errorCode")
    return value.strip() if isinstance(value, str) else None
