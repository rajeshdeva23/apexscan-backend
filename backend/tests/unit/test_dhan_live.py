"""Deterministic tests for the Dhan standard live-feed adapter boundary."""

from __future__ import annotations

import asyncio
import json
import struct
from collections import deque
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.base import (
    NormalizationError,
    ProviderContractViolationError,
    ProviderNetworkError,
    ProviderUnavailableError,
    UnknownProviderReferenceError,
    UnsupportedProviderRequestError,
)
from app.adapters.dhan import DhanRestAdapter
from app.schemas.market_data import (
    DepthSnapshot,
    Instrument,
    MarketDataKind,
    Quote,
    SubscriptionRequest,
    Tick,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "dhan"
_ACCESS_TOKEN = "fixture-live-access-token-must-not-leak"
_CLIENT_ID = "fixture-live-client-id-must-not-leak"


class _FakeLiveSocket:
    """Test-only socket that mirrors the minimal Dhan transport operations."""

    def __init__(self, incoming: tuple[bytes | Exception, ...]) -> None:
        self._incoming = deque(incoming)
        self.sent_payloads: list[dict[str, object]] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent_payloads.append(json.loads(message))

    async def recv(self) -> bytes:
        if not self._incoming:
            raise ConnectionError("fixture feed closed")
        next_message = self._incoming.popleft()
        if isinstance(next_message, Exception):
            raise next_message
        return next_message

    async def close(self) -> None:
        self.closed = True


class _FakeLiveTransport:
    """Injectable network-free transport retaining only test-visible connection metadata."""

    def __init__(self, *sockets: _FakeLiveSocket) -> None:
        self._sockets = deque(sockets)
        self.connection_urls: list[str] = []
        self.timeout_seconds: list[float] = []

    async def connect(self, url: str, timeout_seconds: float) -> _FakeLiveSocket:
        self.connection_urls.append(url)
        self.timeout_seconds.append(timeout_seconds)
        if not self._sockets:
            raise ConnectionError("fixture connection exhausted")
        return self._sockets.popleft()


class _BlockingLiveSocket(_FakeLiveSocket):
    """Socket double that waits in receive until the test cancels its consumer."""

    def __init__(self) -> None:
        super().__init__(())
        self.receive_started = asyncio.Event()
        self._receive_gate = asyncio.Event()

    async def recv(self) -> bytes:
        self.receive_started.set()
        await self._receive_gate.wait()
        raise AssertionError("blocked receive was unexpectedly released")


def _dhan() -> Any:
    """Import the Dhan adapter namespace only after each test declares its contract."""
    try:
        return import_module("app.adapters.dhan")
    except ModuleNotFoundError:
        pytest.fail("P3.4 must provide the Dhan standard live-feed implementation")


def _text(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _packet(name: str) -> bytes:
    return bytes.fromhex(_text(name).strip())


def _master_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "images.dhan.co":
        return httpx.Response(200, text=_text("instrument_master_production_universe.csv"))
    raise AssertionError(f"unexpected REST request: {request.url}")


def test_quote_subscription_planning_sorts_and_batches_cash_equity_references() -> None:
    """Request order or an oversized batch must not create duplicate or rejected requests."""
    dhan = _dhan()
    references = dhan.normalize_instrument_master(
        _text("instrument_master_production_universe.csv")
    )
    live_universe = dhan.resolve_nse_cash_equity_live_universe(references)
    request = SubscriptionRequest(
        instruments=tuple(
            reversed(tuple(reference.instrument for reference in live_universe.cash_references))
        ),
        data_types=frozenset({MarketDataKind.TICK}),
    )

    plan = dhan.plan_live_subscription_batches(
        request,
        live_universe.cash_references,
        maximum_instruments_per_request=2,
    )

    assert plan.feed_mode is dhan.DhanLiveFeedMode.QUOTE
    assert [batch.as_request_payload() for batch in plan.batches] == [
        {
            "RequestCode": 17,
            "InstrumentCount": 2,
            "InstrumentList": [
                {"ExchangeSegment": "NSE_EQ", "SecurityId": "101"},
                {"ExchangeSegment": "NSE_EQ", "SecurityId": "102"},
            ],
        },
        {
            "RequestCode": 17,
            "InstrumentCount": 1,
            "InstrumentList": [
                {"ExchangeSegment": "NSE_EQ", "SecurityId": "103"},
            ],
        },
    ]


def test_documented_standard_feed_limit_yields_a_deterministic_208_plan() -> None:
    """The production universe must fit one connection in documented bounded batches."""
    dhan = _dhan()
    references = tuple(
        dhan.DhanInstrumentReference(
            instrument=Instrument(exchange="NSE", symbol=f"SAFE{index:03d}"),
            security_id=str(index + 1),
            underlying_security_id=None,
            exchange_segment="NSE_EQ",
            provider_instrument_type="ES",
        )
        for index in range(208)
    )
    request = SubscriptionRequest(
        instruments=tuple(reference.instrument for reference in reversed(references)),
        data_types=frozenset({MarketDataKind.TICK}),
    )

    plan = dhan.plan_live_subscription_batches(request, references)

    assert [len(batch.references) for batch in plan.batches] == [100, 100, 8]
    assert plan.feed_mode is dhan.DhanLiveFeedMode.QUOTE


def test_quote_packet_normalizes_only_to_a_canonical_tick() -> None:
    """Treating Dhan Quote trade data as bid/ask data would fabricate a canonical quote."""
    dhan = _dhan()
    references = dhan.normalize_instrument_master(
        _text("instrument_master_production_universe.csv")
    )
    live_universe = dhan.resolve_nse_cash_equity_live_universe(references)

    events = dhan.decode_standard_live_packet(
        _packet("live_quote_packet.hex"), live_universe.cash_references
    )

    assert len(events) == 1
    assert isinstance(events[0], Tick)
    assert events[0].instrument.symbol == "360ONE"
    assert str(events[0].last_price) == "101.25"
    assert events[0].traded_quantity == 12
    assert events[0].event_timestamp.isoformat() == "2025-09-04T06:35:00+00:00"


def test_full_packet_normalizes_quote_and_five_level_depth_when_requested() -> None:
    """Treating a trade-only Quote packet as book data would fabricate bid/ask values."""
    dhan = _dhan()
    references = dhan.normalize_instrument_master(
        _text("instrument_master_production_universe.csv")
    )
    live_universe = dhan.resolve_nse_cash_equity_live_universe(references)
    abb_reference = next(
        reference
        for reference in live_universe.cash_references
        if reference.instrument.symbol == "ABB"
    )
    request = SubscriptionRequest(
        instruments=(abb_reference.instrument,),
        data_types=frozenset({MarketDataKind.TICK, MarketDataKind.QUOTE, MarketDataKind.DEPTH}),
    )

    plan = dhan.plan_live_subscription_batches(request, (abb_reference,))
    events = dhan.decode_standard_live_packet(_packet("live_full_packet.hex"), (abb_reference,))

    assert plan.feed_mode is dhan.DhanLiveFeedMode.FULL
    assert [type(event) for event in events] == [Tick, Quote, DepthSnapshot]
    tick, quote, depth = events
    assert tick.last_price == 200.5
    assert quote.bid_price == 200.0
    assert str(quote.ask_price) == "200.0500030517578"
    assert quote.bid_quantity == 100
    assert quote.ask_quantity == 120
    assert len(depth.bids) == len(depth.asks) == 5
    assert depth.bids[4].quantity == 60
    assert depth.asks[4].price == 200.25


def test_previous_close_packet_is_validated_then_ignored() -> None:
    """Forwarding a non-canonical previous-close packet would invent a live event type."""
    dhan = _dhan()
    references = dhan.normalize_instrument_master(
        _text("instrument_master_production_universe.csv")
    )
    live_universe = dhan.resolve_nse_cash_equity_live_universe(references)

    events = dhan.decode_standard_live_packet(
        _packet("live_previous_close_packet.hex"),
        live_universe.cash_references,
    )

    assert events == ()


async def test_live_stream_reuses_adapter_token_and_unsubscribes_on_generator_cleanup() -> None:
    """An independent auth path or retained active subscription would leak live resources."""
    socket = _FakeLiveSocket((_packet("live_quote_packet.hex"),))
    live_transport = _FakeLiveTransport(socket)
    adapter = DhanRestAdapter(
        access_token=SecretStr(_ACCESS_TOKEN),
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(_master_handler),
        websocket_transport=live_transport,
    )
    await adapter.connect()
    await adapter.load_instruments()
    try:
        live_universe = adapter.load_nse_cash_equity_live_universe()
        request = SubscriptionRequest(
            instruments=(live_universe.cash_references[0].instrument,),
            data_types=frozenset({MarketDataKind.TICK}),
        )
        stream = adapter.stream_market_data(request)
        event = await anext(stream)
        await stream.aclose()
    finally:
        await adapter.disconnect()

    assert isinstance(event, Tick)
    assert socket.sent_payloads == [
        {
            "RequestCode": 17,
            "InstrumentCount": 1,
            "InstrumentList": [{"ExchangeSegment": "NSE_EQ", "SecurityId": "101"}],
        },
        {
            "RequestCode": 18,
            "InstrumentCount": 1,
            "InstrumentList": [{"ExchangeSegment": "NSE_EQ", "SecurityId": "101"}],
        },
        {"RequestCode": 12},
    ]
    query = parse_qs(urlsplit(live_transport.connection_urls[0]).query)
    assert query == {
        "version": ["2"],
        "token": [_ACCESS_TOKEN],
        "clientId": [_CLIENT_ID],
        "authType": ["2"],
    }
    assert live_transport.timeout_seconds == [10.0]
    assert socket.closed is True


async def test_live_stream_reconnects_once_and_restores_desired_subscription() -> None:
    """Dropping desired state after disconnect would leave a healthy socket with no market data."""
    first_socket = _FakeLiveSocket((ConnectionError("fixture feed lost"),))
    second_socket = _FakeLiveSocket((_packet("live_quote_packet.hex"),))
    live_transport = _FakeLiveTransport(first_socket, second_socket)
    sleep_delays: list[float] = []

    async def sleep(delay: float) -> None:
        sleep_delays.append(delay)

    dhan = _dhan()
    adapter = DhanRestAdapter(
        access_token=SecretStr(_ACCESS_TOKEN),
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(_master_handler),
        websocket_transport=live_transport,
        live_reconnect_policy=dhan.DhanLiveReconnectPolicy(
            maximum_attempts=1,
            initial_delay_seconds=0.25,
            maximum_delay_seconds=1.0,
            jitter_ratio=0.0,
        ),
        live_sleep=sleep,
    )
    await adapter.connect()
    await adapter.load_instruments()
    try:
        request = SubscriptionRequest(
            instruments=(
                adapter.load_nse_cash_equity_live_universe().cash_references[0].instrument,
            ),
            data_types=frozenset({MarketDataKind.TICK}),
        )
        stream = adapter.stream_market_data(request)
        event = await anext(stream)
        health = await adapter.get_health()
        await stream.aclose()
    finally:
        await adapter.disconnect()

    assert isinstance(event, Tick)
    assert health.status.value == "healthy"
    assert len(live_transport.connection_urls) == 2
    assert sleep_delays == [0.25]
    assert first_socket.sent_payloads[0]["RequestCode"] == 17
    assert second_socket.sent_payloads == [
        {
            "RequestCode": 17,
            "InstrumentCount": 1,
            "InstrumentList": [{"ExchangeSegment": "NSE_EQ", "SecurityId": "101"}],
        },
        {
            "RequestCode": 18,
            "InstrumentCount": 1,
            "InstrumentList": [{"ExchangeSegment": "NSE_EQ", "SecurityId": "101"}],
        },
        {"RequestCode": 12},
    ]


async def test_identical_live_consumers_share_one_provider_subscription_until_last_cleanup() -> (
    None
):
    """Unsubscribing when any one consumer exits would silently starve the remaining consumer."""
    socket = _FakeLiveSocket(
        (
            _packet("live_quote_packet.hex"),
            _packet("live_quote_packet.hex"),
        )
    )
    adapter = DhanRestAdapter(
        access_token=SecretStr(_ACCESS_TOKEN),
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(_master_handler),
        websocket_transport=_FakeLiveTransport(socket),
    )
    await adapter.connect()
    await adapter.load_instruments()
    try:
        request = SubscriptionRequest(
            instruments=(
                adapter.load_nse_cash_equity_live_universe().cash_references[0].instrument,
            ),
            data_types=frozenset({MarketDataKind.TICK}),
        )
        first_stream = adapter.stream_market_data(request)
        second_stream = adapter.stream_market_data(request)
        assert isinstance(await anext(first_stream), Tick)
        assert isinstance(await anext(second_stream), Tick)

        await second_stream.aclose()
        assert [payload["RequestCode"] for payload in socket.sent_payloads] == [17]

        await first_stream.aclose()
        await first_stream.aclose()
    finally:
        await adapter.disconnect()

    assert [payload["RequestCode"] for payload in socket.sent_payloads] == [17, 18, 12]


async def test_documented_feed_disconnect_packet_reconnects_without_exposing_provider_reason() -> (
    None
):
    """Treating Dhan's disconnect packet as ordinary data would leave the feed falsely healthy."""
    first_socket = _FakeLiveSocket((_packet("live_feed_disconnect_packet.hex"),))
    second_socket = _FakeLiveSocket((_packet("live_quote_packet.hex"),))
    live_transport = _FakeLiveTransport(first_socket, second_socket)

    async def no_wait(_delay: float) -> None:
        return None

    dhan = _dhan()
    adapter = DhanRestAdapter(
        access_token=SecretStr(_ACCESS_TOKEN),
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(_master_handler),
        websocket_transport=live_transport,
        live_reconnect_policy=dhan.DhanLiveReconnectPolicy(
            maximum_attempts=1,
            initial_delay_seconds=0.0,
            maximum_delay_seconds=0.0,
            jitter_ratio=0.0,
        ),
        live_sleep=no_wait,
    )
    await adapter.connect()
    await adapter.load_instruments()
    try:
        request = SubscriptionRequest(
            instruments=(
                adapter.load_nse_cash_equity_live_universe().cash_references[0].instrument,
            ),
            data_types=frozenset({MarketDataKind.TICK}),
        )
        stream = adapter.stream_market_data(request)
        event = await anext(stream)
        await stream.aclose()
    finally:
        await adapter.disconnect()

    assert isinstance(event, Tick)
    assert len(live_transport.connection_urls) == 2


async def test_production_transport_uses_documented_heartbeat_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different heartbeat window risks false feed health or delayed loss detection."""
    live_module = import_module("app.adapters.dhan.live")
    observed: dict[str, object] = {}
    socket = _FakeLiveSocket(())

    async def connect(url: str, **kwargs: object) -> _FakeLiveSocket:
        observed["url"] = url
        observed.update(kwargs)
        return socket

    monkeypatch.setattr(live_module.websockets, "connect", connect)

    connection = await live_module.WebsocketsDhanLiveTransport().connect(
        "wss://fixture.example/feed",
        timeout_seconds=3.0,
    )

    assert connection is socket
    assert observed == {
        "url": "wss://fixture.example/feed",
        "open_timeout": 3.0,
        "ping_interval": 10.0,
        "ping_timeout": 40.0,
        "close_timeout": 3.0,
    }


@pytest.mark.parametrize(
    ("packet", "error_type"),
    [
        (b"\x04" * 7, NormalizationError),
        (_packet("live_quote_packet.hex")[:-1], NormalizationError),
        (bytes([9]) + _packet("live_quote_packet.hex")[1:], UnsupportedProviderRequestError),
    ],
)
def test_decoder_rejects_malformed_truncated_and_unsupported_packets_safely(
    packet: bytes,
    error_type: type[Exception],
) -> None:
    """Accepting malformed wire data could create false canonical market events."""
    dhan = _dhan()
    references = dhan.normalize_instrument_master(
        _text("instrument_master_production_universe.csv")
    )
    live_universe = dhan.resolve_nse_cash_equity_live_universe(references)

    with pytest.raises(error_type):
        dhan.decode_standard_live_packet(packet, live_universe.cash_references)


def test_unknown_provider_reference_is_rejected_without_disclosing_the_raw_identifier() -> None:
    """Mapping an unknown provider ID to a guessed listing would corrupt canonical identity."""
    dhan = _dhan()
    references = dhan.normalize_instrument_master(
        _text("instrument_master_production_universe.csv")
    )
    live_universe = dhan.resolve_nse_cash_equity_live_universe(references)
    packet = bytearray(_packet("live_quote_packet.hex"))
    struct.pack_into("<i", packet, 4, 999)

    with pytest.raises(UnknownProviderReferenceError) as captured:
        dhan.decode_standard_live_packet(bytes(packet), live_universe.cash_references)

    assert "999" not in str(captured.value)


async def test_reconnect_exhaustion_stops_after_the_bounded_policy_and_degrades_health() -> None:
    """An unbounded reconnect loop would hide a provider outage and prevent controlled recovery."""
    first_socket = _FakeLiveSocket((ConnectionError("fixture feed lost"),))
    live_transport = _FakeLiveTransport(first_socket)
    sleep_delays: list[float] = []

    async def sleep(delay: float) -> None:
        sleep_delays.append(delay)

    dhan = _dhan()
    adapter = DhanRestAdapter(
        access_token=SecretStr(_ACCESS_TOKEN),
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(_master_handler),
        websocket_transport=live_transport,
        live_reconnect_policy=dhan.DhanLiveReconnectPolicy(
            maximum_attempts=2,
            initial_delay_seconds=0.25,
            maximum_delay_seconds=1.0,
            jitter_ratio=0.0,
        ),
        live_sleep=sleep,
    )
    await adapter.connect()
    await adapter.load_instruments()
    try:
        request = SubscriptionRequest(
            instruments=(
                adapter.load_nse_cash_equity_live_universe().cash_references[0].instrument,
            ),
            data_types=frozenset({MarketDataKind.TICK}),
        )
        stream = adapter.stream_market_data(request)
        with pytest.raises(ProviderUnavailableError):
            await anext(stream)
        health = await adapter.get_health()
        await stream.aclose()
    finally:
        await adapter.disconnect()

    assert len(live_transport.connection_urls) == 3
    assert sleep_delays == [0.25, 0.5]
    assert health.status.value == "degraded"


async def test_cancelling_a_live_consumer_unsubscribes_without_reconnect() -> None:
    """Converting cancellation into a reconnect would retain an unwanted provider subscription."""
    socket = _BlockingLiveSocket()
    adapter = DhanRestAdapter(
        access_token=SecretStr(_ACCESS_TOKEN),
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(_master_handler),
        websocket_transport=_FakeLiveTransport(socket),
    )
    await adapter.connect()
    await adapter.load_instruments()
    try:
        request = SubscriptionRequest(
            instruments=(
                adapter.load_nse_cash_equity_live_universe().cash_references[0].instrument,
            ),
            data_types=frozenset({MarketDataKind.TICK}),
        )
        stream = adapter.stream_market_data(request)
        consumer = asyncio.create_task(anext(stream))
        await socket.receive_started.wait()
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        await stream.aclose()
    finally:
        await adapter.disconnect()

    assert [payload["RequestCode"] for payload in socket.sent_payloads] == [17, 18, 12]


async def test_live_transport_failure_redacts_credentials_from_the_boundary_error() -> None:
    """Propagating a transport exception would expose the credential-bearing WebSocket URL."""

    class FailingTransport:
        async def connect(self, url: str, timeout_seconds: float) -> _FakeLiveSocket:
            raise RuntimeError(f"failed {url}; token={_ACCESS_TOKEN}; client={_CLIENT_ID}")

    adapter = DhanRestAdapter(
        access_token=SecretStr(_ACCESS_TOKEN),
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(_master_handler),
        websocket_transport=FailingTransport(),
    )
    await adapter.connect()
    await adapter.load_instruments()
    try:
        request = SubscriptionRequest(
            instruments=(
                adapter.load_nse_cash_equity_live_universe().cash_references[0].instrument,
            ),
            data_types=frozenset({MarketDataKind.TICK}),
        )
        stream = adapter.stream_market_data(request)
        with pytest.raises(ProviderNetworkError) as captured:
            await anext(stream)
        await stream.aclose()
    finally:
        await adapter.disconnect()

    diagnostic = str(captured.value)
    assert _ACCESS_TOKEN not in diagnostic
    assert _CLIENT_ID not in diagnostic
    assert "api-feed.dhan.co" not in diagnostic


async def test_mapping_gate_blocks_websocket_connect_when_a_cash_reference_is_missing() -> None:
    """Opening a feed with a partial universe would silently weaken the scanner domain."""
    master = _text("instrument_master_production_universe.csv")
    master += (
        "NSE,D,999,FUTSTK,998,APEXMISSING,APEXMISSING26AUGFUT,APEX MISSING FUT,"
        "FUT,NA,1,2026-08-27,0,\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "images.dhan.co"
        return httpx.Response(200, text=master)

    live_transport = _FakeLiveTransport()
    adapter = DhanRestAdapter(
        access_token=SecretStr(_ACCESS_TOKEN),
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(handler),
        websocket_transport=live_transport,
    )
    await adapter.connect()
    await adapter.load_instruments()
    try:
        request = SubscriptionRequest(
            instruments=(
                adapter.load_nse_cash_equity_live_universe().cash_references[0].instrument,
            ),
            data_types=frozenset({MarketDataKind.TICK}),
        )
        stream = adapter.stream_market_data(request)
        with pytest.raises(ProviderContractViolationError):
            await anext(stream)
        await stream.aclose()
    finally:
        await adapter.disconnect()

    assert live_transport.connection_urls == []


def _packet_with_header(
    name: str,
    *,
    response_code: int | None = None,
    security_id: int | None = None,
) -> bytes:
    """Return a fixture packet with patched header fields for boundary-rejection tests."""
    raw = bytearray(_packet(name))
    if response_code is not None:
        raw[0] = response_code
    if security_id is not None:
        struct.pack_into("<i", raw, 4, security_id)
    return bytes(raw)


def _live_adapter(socket: _FakeLiveSocket) -> DhanRestAdapter:
    """Build a network-free Dhan adapter driven by one fake live socket."""
    return DhanRestAdapter(
        access_token=SecretStr(_ACCESS_TOKEN),
        live_client_id=SecretStr(_CLIENT_ID),
        transport=httpx.MockTransport(_master_handler),
        websocket_transport=_FakeLiveTransport(socket),
    )


async def test_fan_out_delivers_one_frame_to_every_registered_consumer() -> None:
    """M2: a frame read by any consumer reaches every consumer from one subscription."""
    socket = _FakeLiveSocket((_packet("live_quote_packet.hex"), _packet("live_quote_packet.hex")))
    adapter = _live_adapter(socket)
    await adapter.connect()
    await adapter.load_instruments()
    try:
        instrument = adapter.load_nse_cash_equity_live_universe().cash_references[0].instrument
        request = SubscriptionRequest(
            instruments=(instrument,), data_types=frozenset({MarketDataKind.TICK})
        )
        first = adapter.stream_market_data(request)
        second = adapter.stream_market_data(request)

        first_initial = await anext(first)  # registers first, reads frame 1
        second_shared = await anext(second)  # registers second, reads frame 2 -> both buffers
        first_shared = await anext(first)  # drains frame 2 from its own buffer, no extra read

        # Two consumers on one instrument produce exactly one provider subscription.
        assert socket.sent_payloads == [
            {
                "RequestCode": 17,
                "InstrumentCount": 1,
                "InstrumentList": [{"ExchangeSegment": "NSE_EQ", "SecurityId": "101"}],
            }
        ]
        await first.aclose()
        await second.aclose()
    finally:
        await adapter.disconnect()

    assert all(isinstance(event, Tick) for event in (first_initial, second_shared, first_shared))
    # Frame 2 was read once (by the second consumer) yet reached both consumers.
    assert first_shared.event_timestamp == second_shared.event_timestamp
    assert [payload["RequestCode"] for payload in socket.sent_payloads] == [17, 18, 12]


async def test_fan_out_isolates_each_consumer_to_its_own_instrument() -> None:
    """M2: each consumer receives only its own instrument's events, never another's."""
    dhan = _dhan()
    references = dhan.normalize_instrument_master(
        _text("instrument_master_production_universe.csv")
    )
    cash_references = dhan.resolve_nse_cash_equity_live_universe(references).cash_references
    first_reference, second_reference = cash_references[0], cash_references[1]
    socket = _FakeLiveSocket(
        (
            _packet("live_quote_packet.hex"),
            _packet_with_header(
                "live_quote_packet.hex", security_id=int(second_reference.security_id)
            ),
        )
    )
    adapter = _live_adapter(socket)
    await adapter.connect()
    await adapter.load_instruments()
    try:
        first_request = SubscriptionRequest(
            instruments=(first_reference.instrument,), data_types=frozenset({MarketDataKind.TICK})
        )
        second_request = SubscriptionRequest(
            instruments=(second_reference.instrument,), data_types=frozenset({MarketDataKind.TICK})
        )
        first = adapter.stream_market_data(first_request)
        second = adapter.stream_market_data(second_request)

        first_event = await anext(first)  # reads frame for the first instrument only
        second_event = await anext(second)  # reads frame for the second instrument only

        assert first_event.instrument.symbol == first_reference.instrument.symbol
        assert second_event.instrument.symbol == second_reference.instrument.symbol
        assert first_event.instrument.symbol != second_event.instrument.symbol
        await first.aclose()
        await second.aclose()
    finally:
        await adapter.disconnect()


@pytest.mark.parametrize(
    "bad_frame",
    [
        pytest.param(lambda: _packet("live_quote_packet.hex")[:-1], id="truncated"),
        pytest.param(
            lambda: _packet_with_header("live_quote_packet.hex", response_code=9),
            id="unsupported-type",
        ),
        pytest.param(
            lambda: _packet_with_header("live_quote_packet.hex", security_id=999),
            id="unknown-reference",
        ),
    ],
)
async def test_malformed_frame_is_discarded_and_stream_continues(
    bad_frame: Any,
) -> None:
    """M1: a bad frame is dropped and a following valid frame is still delivered."""
    socket = _FakeLiveSocket((bad_frame(), _packet("live_quote_packet.hex")))
    adapter = _live_adapter(socket)
    await adapter.connect()
    await adapter.load_instruments()
    try:
        instrument = adapter.load_nse_cash_equity_live_universe().cash_references[0].instrument
        request = SubscriptionRequest(
            instruments=(instrument,), data_types=frozenset({MarketDataKind.TICK})
        )
        stream = adapter.stream_market_data(request)
        event = await anext(stream)
        await stream.aclose()
    finally:
        await adapter.disconnect()

    assert isinstance(event, Tick)
    assert event.instrument.symbol == "360ONE"
