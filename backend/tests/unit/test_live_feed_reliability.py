"""Deterministic tests for DEPLOY-8.5 live-feed reliability hardening.

Covers the connected-but-silent stale-feed watchdog (session-gated), its reuse of the
existing bounded reconnect, and the two shutdown-cleanup fragilities. No network, no Dhan
credentials — all behaviour is driven through injected fakes, clocks, and predicates.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.base import ProviderNetworkError, ProviderUnavailableError
from app.adapters.dhan import DhanRestAdapter
from app.adapters.dhan.adapter import _LiveFeedStaleError
from app.adapters.dhan.live import DhanLiveReconnectPolicy
from app.core.config import Settings
from app.market_engine.clock import ManualClock
from app.market_engine.sequence import MonotonicSequence
from app.schemas.market_data import (
    FeedContinuity,
    FeedContinuityEvent,
    MarketDataKind,
    ProviderStatus,
    SubscriptionRequest,
    Tick,
)
from app.services.market_runtime import LiveMarketRuntime, RuntimeState

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "dhan"
_ACCESS_TOKEN = "fixture-live-access-token-must-not-leak"
_CLIENT_ID = "fixture-live-client-id-must-not-leak"
_DB = "postgresql+asyncpg://user:pass@localhost:5432/apexscan"
_REDIS = "redis://localhost:6379/0"


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


class _SilentSocket:
    """Connected-but-silent socket: recv blocks forever (until the watchdog fires)."""

    def __init__(self) -> None:
        self.closed = False
        self.sent_payloads: list[dict[str, object]] = []

    async def send(self, message: str) -> None:
        self.sent_payloads.append(json.loads(message))

    async def recv(self) -> bytes:
        await asyncio.Event().wait()  # never resolves; cancelled by the stale deadline
        raise AssertionError("unreachable")

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
# Stale-feed watchdog: connected-but-silent detection + reuse of reconnect
# --------------------------------------------------------------------------- #
async def test_silent_feed_during_live_session_triggers_exactly_one_reconnect() -> None:
    """A socket that stays open but delivers no ticks in a live session must reconnect once."""
    continuity: list[FeedContinuityEvent] = []
    transport = _FakeLiveTransport(
        _SilentSocket(), _FakeLiveSocket((_packet("live_quote_packet.hex"),))
    )
    adapter = DhanRestAdapter(
        access_token=SecretStr(_ACCESS_TOKEN),
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(_master_handler),
        websocket_transport=transport,
        live_reconnect_policy=DhanLiveReconnectPolicy(
            maximum_attempts=1,
            initial_delay_seconds=0.0,
            maximum_delay_seconds=0.0,
            jitter_ratio=0.0,
        ),
        live_sleep=_no_wait,
        live_stale_timeout_seconds=0.05,
        live_session_predicate=lambda: True,
        live_continuity_sink=continuity.append,
    )
    await adapter.connect()
    await adapter.load_instruments()
    try:
        stream = adapter.stream_market_data(_tick_request(adapter))
        event = await asyncio.wait_for(anext(stream), timeout=2.0)
        await stream.aclose()
    finally:
        await adapter.disconnect()

    assert isinstance(event, Tick)
    assert len(transport.connection_urls) == 2  # one bounded reconnect
    statuses = [c.status for c in continuity]
    assert FeedContinuity.CONTINUITY_LOST in statuses
    assert FeedContinuity.RECONNECTED in statuses


async def test_silent_feed_outside_live_session_never_reconnects() -> None:
    """Market-closed silence (predicate False) must not be mistaken for a stale live feed."""
    transport = _FakeLiveTransport(_SilentSocket())
    adapter = DhanRestAdapter(
        access_token=SecretStr(_ACCESS_TOKEN),
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(_master_handler),
        websocket_transport=transport,
        live_sleep=_no_wait,
        live_stale_timeout_seconds=0.05,
        live_session_predicate=lambda: False,  # market closed
    )
    await adapter.connect()
    await adapter.load_instruments()
    try:
        stream = adapter.stream_market_data(_tick_request(adapter))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(anext(stream), timeout=0.2)
        await stream.aclose()
    finally:
        await adapter.disconnect()

    assert len(transport.connection_urls) == 1  # no reconnect while the market is closed


async def test_healthy_feed_never_triggers_the_watchdog() -> None:
    """Continuously arriving ticks keep the feed fresh; the watchdog stays silent."""
    socket = _FakeLiveSocket((_packet("live_quote_packet.hex"), _packet("live_quote_packet.hex")))
    transport = _FakeLiveTransport(socket)
    adapter = DhanRestAdapter(
        access_token=SecretStr(_ACCESS_TOKEN),
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(_master_handler),
        websocket_transport=transport,
        live_stale_timeout_seconds=0.5,
        live_session_predicate=lambda: True,
    )
    await adapter.connect()
    await adapter.load_instruments()
    try:
        stream = adapter.stream_market_data(_tick_request(adapter))
        first = await asyncio.wait_for(anext(stream), timeout=2.0)
        second = await asyncio.wait_for(anext(stream), timeout=2.0)
        await stream.aclose()
    finally:
        await adapter.disconnect()

    assert isinstance(first, Tick)
    assert isinstance(second, Tick)
    assert len(transport.connection_urls) == 1  # no reconnect occurred


async def test_stale_reconnect_exhaustion_is_bounded() -> None:
    """A silent feed whose reconnect cannot re-establish fails closed after the bounded policy."""
    transport = _FakeLiveTransport(_SilentSocket())  # no socket left for the reconnect attempt
    adapter = DhanRestAdapter(
        access_token=SecretStr(_ACCESS_TOKEN),
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(_master_handler),
        websocket_transport=transport,
        live_reconnect_policy=DhanLiveReconnectPolicy(
            maximum_attempts=2,
            initial_delay_seconds=0.0,
            maximum_delay_seconds=0.0,
            jitter_ratio=0.0,
        ),
        live_sleep=_no_wait,
        live_stale_timeout_seconds=0.05,
        live_session_predicate=lambda: True,
    )
    await adapter.connect()
    await adapter.load_instruments()
    try:
        stream = adapter.stream_market_data(_tick_request(adapter))
        with pytest.raises(ProviderUnavailableError):  # fails closed after bounded attempts
            await asyncio.wait_for(anext(stream), timeout=2.0)
        await stream.aclose()
    finally:
        await adapter.disconnect()

    # initial connect + exactly maximum_attempts reconnect tries, never an unbounded storm
    assert len(transport.connection_urls) == 3


# --------------------------------------------------------------------------- #
# _receive_live_frame: watchdog gating unit cases
# --------------------------------------------------------------------------- #
def _idle_adapter(**kwargs: object) -> DhanRestAdapter:
    return DhanRestAdapter(
        access_token=SecretStr(_ACCESS_TOKEN),
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(_master_handler),
        websocket_transport=_FakeLiveTransport(),
        **kwargs,
    )


async def test_watchdog_inactive_without_timeout_returns_plain_recv() -> None:
    adapter = _idle_adapter()  # no stale timeout configured
    adapter._live_socket = _FakeLiveSocket((b"frame",))  # type: ignore[assignment]
    assert await adapter._receive_live_frame() == b"frame"


async def test_watchdog_inactive_without_predicate_returns_plain_recv() -> None:
    adapter = _idle_adapter(live_stale_timeout_seconds=0.01)  # timeout but no predicate
    adapter._live_socket = _FakeLiveSocket((b"frame",))  # type: ignore[assignment]
    assert await adapter._receive_live_frame() == b"frame"


async def test_watchdog_inactive_when_predicate_false_returns_plain_recv() -> None:
    adapter = _idle_adapter(live_stale_timeout_seconds=0.01, live_session_predicate=lambda: False)
    adapter._live_socket = _FakeLiveSocket((b"frame",))  # type: ignore[assignment]
    assert await adapter._receive_live_frame() == b"frame"


async def test_watchdog_active_raises_stale_on_silent_socket() -> None:
    adapter = _idle_adapter(live_stale_timeout_seconds=0.02, live_session_predicate=lambda: True)
    adapter._live_socket = _SilentSocket()  # type: ignore[assignment]
    with pytest.raises(_LiveFeedStaleError):
        await adapter._receive_live_frame()


# --------------------------------------------------------------------------- #
# Shutdown hardening A — adapter.disconnect closes every resource on failure
# --------------------------------------------------------------------------- #
class _CloseFailsSocket:
    async def send(self, message: str) -> None:
        return None

    async def recv(self) -> bytes:
        raise ConnectionError

    async def close(self) -> None:
        raise OSError("socket close failed")


class _RecordingTokenProvider:
    def __init__(self) -> None:
        self.disconnected = False

    async def get_access_token(self) -> SecretStr:
        return SecretStr("tok")

    async def disconnect(self) -> None:
        self.disconnected = True


async def test_disconnect_closes_http_clients_even_if_socket_close_fails() -> None:
    """A socket-close failure must not leak the HTTP clients or skip the token provider."""
    token_provider = _RecordingTokenProvider()
    adapter = DhanRestAdapter(
        token_provider=token_provider,
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(_master_handler),
        websocket_transport=_FakeLiveTransport(),
    )
    await adapter.connect()
    adapter._live_status = ProviderStatus.HEALTHY
    adapter._live_socket = _CloseFailsSocket()  # type: ignore[assignment]
    api_client = adapter._api_client
    reference_client = adapter._reference_client

    with pytest.raises(ProviderNetworkError):  # the socket-close failure is surfaced
        await adapter.disconnect()

    assert api_client is not None and api_client.is_closed
    assert reference_client is not None and reference_client.is_closed
    assert token_provider.disconnected is True


# --------------------------------------------------------------------------- #
# Shutdown hardening B — runtime.shutdown cleans every resource on failure
# --------------------------------------------------------------------------- #
class _RecordingDetach:
    def __init__(self) -> None:
        self.unsubscribed = False

    def unsubscribe(self) -> None:
        self.unsubscribed = True


async def _raise_on_cancel() -> None:
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise RuntimeError("task cleanup failed") from None


async def test_runtime_shutdown_cleans_all_tasks_when_one_cancel_fails() -> None:
    """One task's cancellation failure must not leave the other managed tasks running."""
    runtime = LiveMarketRuntime(
        settings=Settings(app_env="development", database_url=_DB, redis_url=_REDIS),
        error_threshold=3,
        instruments=(),
        live_market_data=None,
        clock=ManualClock(datetime(2026, 8, 6, 6, 30, tzinfo=UTC)),
        sequence=MonotonicSequence(),
    )
    scanner = _RecordingDetach()
    manager = _RecordingDetach()
    runtime._scanner = scanner  # type: ignore[assignment]
    runtime._manager = manager  # type: ignore[assignment]
    runtime._state = RuntimeState.STARTED
    runtime._manager_subscribed = True

    bad_task = asyncio.create_task(_raise_on_cancel())
    refresh_task = asyncio.create_task(asyncio.Event().wait())
    monitor_task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)  # let every task start
    runtime._ingestion_task = bad_task
    runtime._refresh_driver_task = refresh_task
    runtime._calendar_monitor_task = monitor_task

    with pytest.raises(RuntimeError):  # the failing cancellation is surfaced
        await runtime.shutdown()

    assert refresh_task.cancelled()  # unrelated tasks were still cancelled
    assert monitor_task.cancelled()
    assert scanner.unsubscribed is True  # detach steps still ran
    assert manager.unsubscribed is True
    assert runtime._manager_subscribed is False
