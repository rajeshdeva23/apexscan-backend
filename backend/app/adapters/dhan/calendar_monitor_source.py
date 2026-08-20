"""HTTP retrieval of the secondary Dhan market-holiday page (ADR-011; provider-specific).

Retrieval ONLY: this module makes a single bounded GET against a public, unauthenticated
webpage and returns its text. It carries no credentials, tokens, cookies, PIN, TOTP, or
session state, performs no login, and never logs the URL body or HTML. Parsing,
comparison, and scheduling live elsewhere; scraping stays inside ``app.adapters.dhan``.
"""

from __future__ import annotations

import httpx

from app.adapters.base.errors import ProviderBoundaryError

DHAN_MARKET_HOLIDAY_URL = "https://dhan.co/market-holiday/"
_USER_AGENT = "ApexScanCalendarMonitor/1.0"
_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 300


class CalendarMonitorFetchError(ProviderBoundaryError):
    """Raised when the secondary calendar page cannot be retrieved."""


class DhanMarketHolidaySource:
    """Fetch the public Dhan market-holiday page as raw HTML (single attempt, bounded)."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        url: str = DHAN_MARKET_HOLIDAY_URL,
    ) -> None:
        """Wire the source to a bounded timeout and an optional injected transport.

        Args:
            timeout_seconds: The bounded per-request timeout.
            transport: An optional transport (tests inject ``httpx.MockTransport``); the
                default uses httpx's own transport.
            url: The public page URL; defaults to the module constant.
        """
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._url = url

    @property
    def url(self) -> str:
        """Return the configured public page URL."""
        return self._url

    async def fetch(self) -> str:
        """Retrieve the page HTML with one bounded, unauthenticated GET.

        Returns:
            The response body text.

        Raises:
            CalendarMonitorFetchError: On timeout, transport error, or a non-2xx status.
        """
        timeout = httpx.Timeout(self._timeout_seconds)
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=timeout) as client:
                response = await client.get(self._url, headers={"User-Agent": _USER_AGENT})
        except httpx.TimeoutException as error:
            raise CalendarMonitorFetchError("calendar monitor page request timed out") from error
        except httpx.RequestError as error:
            raise CalendarMonitorFetchError("calendar monitor page request failed") from error
        if not _HTTP_OK_MIN <= response.status_code < _HTTP_OK_MAX:
            raise CalendarMonitorFetchError(
                f"calendar monitor page returned HTTP {response.status_code}"
            )
        return response.text
