"""DEPLOY-9 R2.1 tests: Dhan transport-close recovery + auth rate-limit classification.

Reproduces the two DEPLOY-9 R2 defects offline, with no network and no real credentials:

1. A transport-level WebSocket close (``websockets.exceptions.ConnectionClosed`` /
   ``ConnectionClosedError`` / ``ConnectionClosedOK``) must be translated at the live
   transport boundary into the library-neutral :class:`DhanFeedDisconnectedError` so the
   existing bounded reconnect machinery recovers instead of terminating ingestion.
2. Dhan's token-generation frequency limit, returned as HTTP 200 with an error body, must
   map to :class:`ProviderRateLimitError` (not ``NormalizationError``), while unrelated
   error-shaped 200 bodies keep their existing classification.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import websockets.exceptions as wse
from pydantic import SecretStr

from app.adapters.base.errors import (
    NormalizationError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from app.adapters.dhan import DhanRestAdapter
from app.adapters.dhan.auth import DhanAuthManager
from app.adapters.dhan.live import (
    DhanFeedDisconnectedError,
    DhanLiveReconnectPolicy,
    _TranslatingDhanLiveSocket,
)
from app.schemas.market_data import (
    FeedContinuity,
    FeedContinuityEvent,
    MarketDataKind,
    SubscriptionRequest,
    Tick,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "dhan"
_ACCESS_TOKEN = "fixture-live-access-token-must-not-leak"
_CLIENT_ID = "fixture-live-client-id-must-not-leak"
_VALID_TOTP_SECRET = "JBSWY3DPEHPK3PXP"  # RFC 6238 base32 test vector; not a real secret


def _text(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _packet(name: str) -> bytes:
    return bytes.fromhex(_text(name).strip())


def _master_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "images.dhan.co":
        return httpx.Response(200, text=_text("instrument_master_production_universe.csv"))
    raise AssertionError(f"unexpected REST request: {request.url}")


class _FakeLiveSocket:
    """Programmable socket double: each recv yields the next queued frame or raises."""

    def __init__(self, incoming: tuple[bytes | Exception, ...]) -> None:
        self._incoming = deque(incoming)
        self.sent_payloads: list[dict[str, object]] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent_payloads.append(json.loads(message))

    async def recv(self) -> bytes:
        if not self._incoming:
            raise ConnectionError("fixture feed closed")
        nxt = self._incoming.popleft()
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def close(self) -> None:
        self.closed = True


class _FakeLiveTransport:
    """Hands out preconfigured sockets in order and records connection metadata."""

    def __init__(self, *sockets: object) -> None:
        self._sockets = deque(sockets)
        self.connection_urls: list[str] = []

    async def connect(self, url: str, timeout_seconds: float) -> object:
        self.connection_urls.append(url)
        if not self._sockets:
            raise ConnectionError("fixture connection exhausted")
        return self._sockets.popleft()


async def _no_wait(_delay: float) -> None:
    return None


def _tick_request(adapter: DhanRestAdapter) -> SubscriptionRequest:
    instrument = adapter.load_nse_cash_equity_live_universe().cash_references[0].instrument
    return SubscriptionRequest(
        instruments=(instrument,), data_types=frozenset({MarketDataKind.TICK})
    )


# --------------------------------------------------------------------------- #
# Transport boundary: websockets close -> library-neutral DhanFeedDisconnectedError
# --------------------------------------------------------------------------- #
def test_dhan_feed_disconnected_error_is_a_connection_error() -> None:
    """The reconnect trigger catches ConnectionError; the translated type must qualify."""
    assert issubclass(DhanFeedDisconnectedError, ConnectionError)


@pytest.mark.parametrize(
    "closed",
    [wse.ConnectionClosedError(None, None), wse.ConnectionClosedOK(None, None)],
)
async def test_wrapper_translates_transport_close_on_recv(closed: wse.ConnectionClosed) -> None:
    """A websockets close raised by recv surfaces as DhanFeedDisconnectedError."""

    class _Inner:
        async def send(self, message: str) -> None:
            raise closed

        async def recv(self) -> bytes:
            raise closed

        async def close(self) -> None:
            return None

    socket = _TranslatingDhanLiveSocket(_Inner())
    with pytest.raises(DhanFeedDisconnectedError):
        await socket.recv()
    with pytest.raises(DhanFeedDisconnectedError):
        await socket.send("{}")


async def test_wrapper_passes_through_normal_frames_and_close() -> None:
    """The wrapper is transparent when the underlying socket behaves normally."""
    inner = _FakeLiveSocket((b"frame",))
    socket = _TranslatingDhanLiveSocket(inner)
    assert await socket.recv() == b"frame"
    await socket.send(json.dumps({"RequestCode": 12}))
    assert inner.sent_payloads == [{"RequestCode": 12}]
    await socket.close()
    assert inner.closed is True


# --------------------------------------------------------------------------- #
# Adapter: a transport close during ingestion reuses the existing reconnect path
# --------------------------------------------------------------------------- #
def _recovery_adapter(
    transport: _FakeLiveTransport, sink: list[FeedContinuityEvent], attempts: int
) -> DhanRestAdapter:
    return DhanRestAdapter(
        access_token=SecretStr(_ACCESS_TOKEN),
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(_master_handler),
        websocket_transport=transport,
        live_reconnect_policy=DhanLiveReconnectPolicy(
            maximum_attempts=attempts,
            initial_delay_seconds=0.0,
            maximum_delay_seconds=0.0,
            jitter_ratio=0.0,
        ),
        live_sleep=_no_wait,
        live_continuity_sink=sink.append,
    )


@pytest.mark.parametrize(
    "closed",
    [wse.ConnectionClosedError(None, None), wse.ConnectionClosedOK(None, None)],
)
async def test_transport_close_triggers_reconnect_and_resumes(
    closed: wse.ConnectionClosed,
) -> None:
    """recv raising a websockets close reconnects once and resumes event delivery."""
    continuity: list[FeedContinuityEvent] = []
    inner_bad = _FakeLiveSocket((closed,))
    good = _FakeLiveSocket((_packet("live_quote_packet.hex"),))
    transport = _FakeLiveTransport(_TranslatingDhanLiveSocket(inner_bad), good)
    adapter = _recovery_adapter(transport, continuity, attempts=1)
    await adapter.connect()
    await adapter.load_instruments()
    try:
        stream = adapter.stream_market_data(_tick_request(adapter))
        event = await asyncio.wait_for(anext(stream), timeout=2.0)
        await stream.aclose()
    finally:
        await adapter.disconnect()

    assert isinstance(event, Tick)
    assert len(transport.connection_urls) == 2  # one bounded reconnect, no duplicate socket
    statuses = [c.status for c in continuity]
    assert FeedContinuity.CONTINUITY_LOST in statuses
    assert FeedContinuity.RECONNECTED in statuses
    assert inner_bad.closed is True  # the closed socket is released, not leaked
    subscribe_msgs = [p for p in good.sent_payloads if p.get("RequestCode") in (17, 21)]
    assert len(subscribe_msgs) == 1  # desired plan resubscribed exactly once, no duplicates


async def test_transport_close_reconnect_exhaustion_is_bounded() -> None:
    """A transport close whose reconnect cannot re-establish fails closed, never a storm."""
    continuity: list[FeedContinuityEvent] = []
    inner_bad = _FakeLiveSocket((wse.ConnectionClosedError(None, None),))
    transport = _FakeLiveTransport(_TranslatingDhanLiveSocket(inner_bad))
    adapter = _recovery_adapter(transport, continuity, attempts=2)
    await adapter.connect()
    await adapter.load_instruments()
    try:
        stream = adapter.stream_market_data(_tick_request(adapter))
        with pytest.raises(ProviderUnavailableError):
            await asyncio.wait_for(anext(stream), timeout=2.0)
        await stream.aclose()
    finally:
        await adapter.disconnect()

    assert len(transport.connection_urls) == 3  # initial + exactly maximum_attempts tries


# --------------------------------------------------------------------------- #
# Auth: HTTP 200 error-body classification
# --------------------------------------------------------------------------- #
def _auth_manager(handler: Callable[[httpx.Request], httpx.Response]) -> DhanAuthManager:
    return DhanAuthManager(
        client_id=SecretStr("client"),
        pin=SecretStr("123456"),
        totp_secret=SecretStr(_VALID_TOTP_SECRET),
        transport=httpx.MockTransport(handler),
    )


async def test_generation_rate_limit_200_maps_to_rate_limit_error() -> None:
    """Dhan's 'once every 2 minutes' 200 body is a rate limit, not a normalization failure."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "error", "message": "Token can be generated once every 2 minutes."}
        )

    manager = _auth_manager(handler)
    try:
        with pytest.raises(ProviderRateLimitError):
            await manager.get_access_token()
    finally:
        await manager.disconnect()


async def test_unrelated_error_200_body_is_not_classified_as_rate_limit() -> None:
    """An error-shaped 200 body without the generation-limit signal keeps its classification."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "error", "message": "Invalid credentials"})

    manager = _auth_manager(handler)
    try:
        with pytest.raises(NormalizationError):
            await manager.get_access_token()
    finally:
        await manager.disconnect()


async def test_malformed_200_without_token_still_normalization_error() -> None:
    """A 200 body lacking a token and any error marker stays a normalization failure."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"foo": "bar"})

    manager = _auth_manager(handler)
    try:
        with pytest.raises(NormalizationError):
            await manager.get_access_token()
    finally:
        await manager.disconnect()
