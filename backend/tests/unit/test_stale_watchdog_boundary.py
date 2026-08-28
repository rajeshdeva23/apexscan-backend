"""DEPLOY-9.6 tests: two-threshold stale watchdog (soft suspect / hard reconnect).

Reproduces the DEPLOY-9.5 boundary-churn defect offline: a legitimate low-tick lull that
exceeds the soft threshold but not the hard threshold must NOT reconnect, while a genuinely
stuck feed that exceeds the hard threshold still triggers exactly one bounded reconnect.
No network, no credentials; freshness is driven by a manual monotonic clock (no wall-clock
sleeps except the tiny real ``asyncio.wait_for`` used by the integration reconnect tests).
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.adapters.base import ProviderUnavailableError
from app.adapters.dhan import DhanRestAdapter
from app.adapters.dhan.adapter import _LiveFeedStaleError
from app.adapters.dhan.live import DhanLiveReconnectPolicy
from app.core.config import Settings
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


class _ManualClock:
    """A controllable monotonic clock: advance by setting ``.t``."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class _FakeLiveSocket:
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
    def __init__(self) -> None:
        self.closed = False
        self.sent_payloads: list[dict[str, object]] = []

    async def send(self, message: str) -> None:
        self.sent_payloads.append(json.loads(message))

    async def recv(self) -> bytes:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True


class _FakeLiveTransport:
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


def _wd_adapter(
    *,
    soft: float,
    hard: float,
    clock: _ManualClock | None = None,
    predicate: object = None,
    websocket_transport: object = None,
    reconnect_attempts: int = 1,
) -> DhanRestAdapter:
    kwargs: dict[str, object] = {
        "access_token": SecretStr(_ACCESS_TOKEN),
        "live_client_id": SecretStr(_CLIENT_ID),
        "transport": httpx.MockTransport(_master_handler),
        "live_stale_timeout_seconds": soft,
        "live_hard_stale_timeout_seconds": hard,
        "live_session_predicate": predicate if predicate is not None else (lambda: True),
    }
    if clock is not None:
        kwargs["live_clock"] = clock
    if websocket_transport is not None:
        kwargs["websocket_transport"] = websocket_transport
        kwargs["live_reconnect_policy"] = DhanLiveReconnectPolicy(
            maximum_attempts=reconnect_attempts,
            initial_delay_seconds=0.0,
            maximum_delay_seconds=0.0,
            jitter_ratio=0.0,
        )
        kwargs["live_sleep"] = _no_wait
    return DhanRestAdapter(**kwargs)  # type: ignore[arg-type]


def _tick_request(adapter: DhanRestAdapter) -> SubscriptionRequest:
    instrument = adapter.load_nse_cash_equity_live_universe().cash_references[0].instrument
    return SubscriptionRequest(
        instruments=(instrument,), data_types=frozenset({MarketDataKind.TICK})
    )


# --------------------------------------------------------------------------- #
# TEST A — legitimate lull in [soft, hard): degraded warning, NO reconnect
# --------------------------------------------------------------------------- #
async def test_lull_between_soft_and_hard_does_not_reconnect() -> None:
    clock = _ManualClock()
    adapter = _wd_adapter(soft=30.0, hard=120.0, clock=clock)
    adapter._live_socket = _FakeLiveSocket((b"frame",))  # type: ignore[assignment]
    adapter._last_valid_event_at = 0.0
    clock.t = 60.0  # elapsed 60s: past soft (30), well under hard (120)
    frame = await adapter._receive_live_frame()
    assert frame == b"frame"  # returned normally — no _LiveFeedStaleError
    assert adapter._suspect_stale_logged is True  # suspected-stale logged once


# --------------------------------------------------------------------------- #
# TEST B — prolonged silence past hard threshold: stale (bounded recovery upstream)
# --------------------------------------------------------------------------- #
async def test_silence_past_hard_threshold_raises_stale() -> None:
    clock = _ManualClock()
    adapter = _wd_adapter(soft=30.0, hard=120.0, clock=clock)
    adapter._live_socket = _FakeLiveSocket((b"frame",))  # type: ignore[assignment]
    adapter._last_valid_event_at = 0.0
    clock.t = 120.0  # elapsed >= hard
    with pytest.raises(_LiveFeedStaleError):
        await adapter._receive_live_frame()


# --------------------------------------------------------------------------- #
# TEST C — MARKET_CLOSED (predicate False): never stale regardless of silence
# --------------------------------------------------------------------------- #
async def test_market_closed_never_stale() -> None:
    clock = _ManualClock()
    adapter = _wd_adapter(soft=30.0, hard=120.0, clock=clock, predicate=lambda: False)
    adapter._live_socket = _FakeLiveSocket((b"frame",))  # type: ignore[assignment]
    adapter._last_valid_event_at = 0.0
    clock.t = 100_000.0
    assert await adapter._receive_live_frame() == b"frame"
    assert adapter._suspect_stale_logged is False


# --------------------------------------------------------------------------- #
# TEST D — healthy (elapsed < soft): no warning, no reconnect
# --------------------------------------------------------------------------- #
async def test_healthy_below_soft_no_warning_no_reconnect() -> None:
    clock = _ManualClock()
    adapter = _wd_adapter(soft=30.0, hard=120.0, clock=clock)
    adapter._live_socket = _FakeLiveSocket((b"frame",))  # type: ignore[assignment]
    adapter._last_valid_event_at = 0.0
    clock.t = 10.0  # under soft
    assert await adapter._receive_live_frame() == b"frame"
    assert adapter._suspect_stale_logged is False


# --------------------------------------------------------------------------- #
# TEST E — freshness resumes before hard: a valid event clears the suspect state
# --------------------------------------------------------------------------- #
async def test_valid_event_clears_suspect_state() -> None:
    clock = _ManualClock()
    adapter = _wd_adapter(soft=30.0, hard=120.0, clock=clock)
    await adapter.connect()
    await adapter.load_instruments()
    adapter._live_cash_references = adapter.load_nse_cash_equity_live_universe().cash_references
    adapter._last_valid_event_at = 0.0
    clock.t = 60.0
    adapter._live_socket = _FakeLiveSocket((b"x",))  # type: ignore[assignment]
    await adapter._receive_live_frame()
    assert adapter._suspect_stale_logged is True
    clock.t = 61.0
    adapter._distribute_live_packet(_packet("live_quote_packet.hex"))  # a valid canonical tick
    assert adapter._suspect_stale_logged is False
    assert adapter._last_valid_event_at == 61.0
    await adapter.disconnect()


# --------------------------------------------------------------------------- #
# TEST F — hard-stale drives exactly one bounded reconnect + resubscribe + RECONNECTED
# --------------------------------------------------------------------------- #
async def test_hard_stale_triggers_one_bounded_reconnect() -> None:
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
        live_stale_timeout_seconds=0.02,
        live_hard_stale_timeout_seconds=0.05,
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
    assert len(transport.connection_urls) == 2  # exactly one bounded reconnect
    statuses = [c.status for c in continuity]
    assert FeedContinuity.CONTINUITY_LOST in statuses
    assert FeedContinuity.RECONNECTED in statuses


# --------------------------------------------------------------------------- #
# TEST G — reconnect exhaustion stays bounded (ProviderUnavailableError)
# --------------------------------------------------------------------------- #
async def test_hard_stale_reconnect_exhaustion_is_bounded() -> None:
    transport = _FakeLiveTransport(_SilentSocket())  # nothing left for the reconnect
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
        live_stale_timeout_seconds=0.02,
        live_hard_stale_timeout_seconds=0.05,
        live_session_predicate=lambda: True,
    )
    await adapter.connect()
    await adapter.load_instruments()
    try:
        stream = adapter.stream_market_data(_tick_request(adapter))
        with pytest.raises(ProviderUnavailableError):
            await asyncio.wait_for(anext(stream), timeout=2.0)
        await stream.aclose()
    finally:
        await adapter.disconnect()
    assert len(transport.connection_urls) == 3  # initial + exactly maximum_attempts


# --------------------------------------------------------------------------- #
# TEST H — cancellation during a stale wait propagates cleanly, no orphan task
# --------------------------------------------------------------------------- #
async def test_cancellation_during_watch_is_clean() -> None:
    transport = _FakeLiveTransport(_SilentSocket())
    adapter = DhanRestAdapter(
        access_token=SecretStr(_ACCESS_TOKEN),
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(_master_handler),
        websocket_transport=transport,
        live_sleep=_no_wait,
        live_stale_timeout_seconds=10.0,
        live_hard_stale_timeout_seconds=20.0,  # large: task is mid-wait when cancelled
        live_session_predicate=lambda: True,
    )
    await adapter.connect()
    await adapter.load_instruments()
    stream = adapter.stream_market_data(_tick_request(adapter))
    task = asyncio.create_task(anext(stream))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)  # let it enter the recv wait
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await stream.aclose()
    await adapter.disconnect()


# --------------------------------------------------------------------------- #
# Settings validation — hard must exceed soft
# --------------------------------------------------------------------------- #
def test_hard_threshold_must_exceed_soft() -> None:
    with pytest.raises(ValidationError):
        Settings(
            app_env="development",
            database_url=_DB,
            redis_url=_REDIS,
            dhan_live_stale_timeout_seconds=100.0,
            dhan_live_hard_stale_timeout_seconds=50.0,
        )


def test_default_thresholds_are_ordered() -> None:
    settings = Settings(app_env="development", database_url=_DB, redis_url=_REDIS)
    assert settings.dhan_live_hard_stale_timeout_seconds > settings.dhan_live_stale_timeout_seconds
