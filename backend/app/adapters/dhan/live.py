"""Dhan standard live-feed request planning kept below the provider boundary."""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, cast
from urllib.parse import urlencode

import websockets
from pydantic import ValidationError

from app.adapters.base.errors import (
    NormalizationError,
    ProviderContractViolationError,
    UnknownProviderReferenceError,
    UnsupportedProviderRequestError,
)
from app.adapters.dhan.models import DhanInstrumentReference
from app.schemas.market_data import (
    DepthLevel,
    DepthSnapshot,
    MarketData,
    MarketDataKind,
    ProviderSessionOhlc,
    Quote,
    SubscriptionRequest,
    Tick,
)

_DOCUMENTED_MAXIMUM_INSTRUMENTS_PER_REQUEST = 100
_HEADER = struct.Struct("<B h B i")
_QUOTE_PAYLOAD = struct.Struct("<f h i f i i i f f f f")
_FULL_PAYLOAD = struct.Struct("<f h i f i i i i i i f f f f")
_DEPTH_LEVEL = struct.Struct("<i i h h f f")
_PREVIOUS_CLOSE_PAYLOAD = struct.Struct("<f i")
_FEED_DISCONNECT_PAYLOAD = struct.Struct("<h")
_QUOTE_RESPONSE_CODE = 4
_FULL_RESPONSE_CODE = 8
_PREVIOUS_CLOSE_RESPONSE_CODE = 6
_FEED_DISCONNECT_RESPONSE_CODE = 50
_EXCHANGE_SEGMENT_CODES = {
    "IDX_I": 0,
    "NSE_EQ": 1,
    "NSE_FNO": 2,
    "NSE_CURRENCY": 3,
    "BSE_EQ": 4,
    "MCX_COMM": 5,
    "BSE_CURRENCY": 7,
    "BSE_FNO": 8,
}


class DhanLiveSocket(Protocol):
    """Minimal asynchronous socket operations used by the adapter."""

    async def send(self, message: str) -> None:
        """Send one JSON text frame."""

    async def recv(self) -> bytes | str:
        """Receive one provider frame."""

    async def close(self) -> None:
        """Close the underlying connection idempotently."""


class DhanLiveTransport(Protocol):
    """Injectable Dhan-only connection factory for real and fake transports."""

    async def connect(self, url: str, timeout_seconds: float) -> DhanLiveSocket:
        """Connect without exposing transport details above the Dhan adapter."""


@dataclass(frozen=True, slots=True)
class DhanLiveReconnectPolicy:
    """Bounded reconnect timing for a single standard-feed connection."""

    maximum_attempts: int = 3
    initial_delay_seconds: float = 0.5
    maximum_delay_seconds: float = 5.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if self.maximum_attempts < 0:
            raise ValueError("maximum reconnect attempts must not be negative")
        if (
            self.initial_delay_seconds < 0
            or self.maximum_delay_seconds < self.initial_delay_seconds
        ):
            raise ValueError("reconnect delays must be non-negative and ordered")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("reconnect jitter ratio must be between zero and one")

    def delay_for_attempt(self, attempt: int, random_value: float) -> float:
        """Return the bounded exponential delay for one one-based reconnect attempt."""
        if attempt < 1 or not 0 <= random_value <= 1:
            raise ValueError("reconnect attempt and random value are invalid")
        exponential_delay = self.initial_delay_seconds * (2.0 ** (attempt - 1))
        base_delay = min(
            self.maximum_delay_seconds,
            exponential_delay,
        )
        jitter = (random_value * 2 - 1) * self.jitter_ratio
        return base_delay * (1 + jitter)


class DhanFeedDisconnectedError(ConnectionError):
    """Safe signal that Dhan terminated the standard feed connection."""


class WebsocketsDhanLiveTransport:
    """Production WebSocket transport with library-managed server ping/pong responses."""

    async def connect(self, url: str, timeout_seconds: float) -> DhanLiveSocket:
        """Open one standard Dhan feed connection without logging its credential-bearing URL."""
        connection = await websockets.connect(
            url,
            open_timeout=timeout_seconds,
            ping_interval=10.0,
            ping_timeout=40.0,
            close_timeout=timeout_seconds,
        )
        return cast(DhanLiveSocket, connection)


class DhanLiveFeedMode(StrEnum):
    """Standard Dhan feed modes supported by the P3.4 adapter."""

    QUOTE = "quote"
    FULL = "full"


_SUBSCRIBE_REQUEST_CODES = {
    DhanLiveFeedMode.QUOTE: 17,
    DhanLiveFeedMode.FULL: 21,
}
_UNSUBSCRIBE_REQUEST_CODES = {
    DhanLiveFeedMode.QUOTE: 18,
    DhanLiveFeedMode.FULL: 22,
}
_DISCONNECT_REQUEST_CODE = 12


@dataclass(frozen=True, slots=True)
class DhanLiveSubscriptionBatch:
    """One provider-private Dhan JSON subscription batch."""

    feed_mode: DhanLiveFeedMode
    references: tuple[DhanInstrumentReference, ...]

    def as_request_payload(self, *, unsubscribe: bool = False) -> dict[str, object]:
        """Return the documented JSON request without exposing it above the adapter."""
        request_codes = _UNSUBSCRIBE_REQUEST_CODES if unsubscribe else _SUBSCRIBE_REQUEST_CODES
        return {
            "RequestCode": request_codes[self.feed_mode],
            "InstrumentCount": len(self.references),
            "InstrumentList": [
                {
                    "ExchangeSegment": reference.exchange_segment,
                    "SecurityId": reference.security_id,
                }
                for reference in self.references
            ],
        }


@dataclass(frozen=True, slots=True)
class DhanLiveSubscriptionPlan:
    """Deterministic standard-feed batches for one canonical subscription request."""

    feed_mode: DhanLiveFeedMode
    batches: tuple[DhanLiveSubscriptionBatch, ...]


def build_standard_live_url(*, access_token: str, client_id: str) -> str:
    """Build the documented Dhan v2 feed URL for adapter-internal transport use only."""
    return "wss://api-feed.dhan.co?" + urlencode(
        {
            "version": "2",
            "token": access_token,
            "clientId": client_id,
            "authType": "2",
        }
    )


def encode_live_request(payload: dict[str, object]) -> str:
    """Encode one validated provider-private request as deterministic compact JSON."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def encode_live_disconnect_request() -> str:
    """Encode Dhan's documented graceful standard-feed disconnect request."""
    return encode_live_request({"RequestCode": _DISCONNECT_REQUEST_CODE})


def plan_live_subscription_batches(
    request: SubscriptionRequest,
    cash_references: tuple[DhanInstrumentReference, ...],
    *,
    maximum_instruments_per_request: int = _DOCUMENTED_MAXIMUM_INSTRUMENTS_PER_REQUEST,
) -> DhanLiveSubscriptionPlan:
    """Resolve canonical cash instruments into documented, bounded Dhan request batches."""
    if not 1 <= maximum_instruments_per_request <= _DOCUMENTED_MAXIMUM_INSTRUMENTS_PER_REQUEST:
        raise ProviderContractViolationError()

    feed_mode = _feed_mode_for(request)
    references_by_instrument = {reference.instrument: reference for reference in cash_references}
    if len(references_by_instrument) != len(cash_references):
        raise ProviderContractViolationError()

    try:
        requested_references = tuple(
            references_by_instrument[instrument] for instrument in request.instruments
        )
    except KeyError as error:
        raise UnsupportedProviderRequestError() from error

    sorted_references = tuple(sorted(requested_references, key=_reference_sort_key))
    return DhanLiveSubscriptionPlan(
        feed_mode=feed_mode,
        batches=tuple(
            DhanLiveSubscriptionBatch(
                feed_mode=feed_mode,
                references=sorted_references[start : start + maximum_instruments_per_request],
            )
            for start in range(0, len(sorted_references), maximum_instruments_per_request)
        ),
    )


def decode_standard_live_packet(
    packet: bytes,
    cash_references: tuple[DhanInstrumentReference, ...],
) -> tuple[MarketData, ...]:
    """Decode one supported standard-feed packet without leaking Dhan wire values."""
    if len(packet) < _HEADER.size:
        raise NormalizationError()
    response_code, message_length, exchange_segment_code, security_id = _HEADER.unpack_from(packet)
    if (
        message_length != len(packet)
        or exchange_segment_code not in _EXCHANGE_SEGMENT_CODES.values()
    ):
        raise NormalizationError()
    if response_code == _FEED_DISCONNECT_RESPONSE_CODE:
        _decode_feed_disconnect_packet(packet)
        raise DhanFeedDisconnectedError("Dhan live feed disconnected")
    if security_id <= 0:
        raise NormalizationError()
    reference = _resolve_reference(cash_references, exchange_segment_code, security_id)
    if response_code == _QUOTE_RESPONSE_CODE:
        return _decode_quote_packet(packet, reference)
    if response_code == _FULL_RESPONSE_CODE:
        return _decode_full_packet(packet, reference)
    if response_code == _PREVIOUS_CLOSE_RESPONSE_CODE:
        return _decode_previous_close_packet(packet)
    raise UnsupportedProviderRequestError()


def _decode_quote_packet(
    packet: bytes,
    reference: DhanInstrumentReference,
) -> tuple[MarketData, ...]:
    if len(packet) != _HEADER.size + _QUOTE_PAYLOAD.size:
        raise NormalizationError()
    try:
        (
            last_price,
            last_trade_quantity,
            last_trade_time,
            average_trade_price,
            volume,
            total_sell_quantity,
            total_buy_quantity,
            day_open,
            day_close,
            day_high,
            day_low,
        ) = _QUOTE_PAYLOAD.unpack_from(packet, _HEADER.size)
        _require_positive_finite(last_price)
        _require_nonnegative_integral(
            last_trade_quantity,
            volume,
            total_sell_quantity,
            total_buy_quantity,
        )
        _require_nonnegative_finite(average_trade_price, day_open, day_close, day_high, day_low)
        event_timestamp = _epoch_timestamp(last_trade_time)
        return (
            Tick(
                instrument=reference.instrument,
                event_timestamp=event_timestamp,
                last_price=Decimal(str(last_price)),
                traded_quantity=last_trade_quantity,
                session_cumulative_volume=volume,
                session_ohlc=_session_ohlc(day_open, day_high, day_low, day_close),
            ),
        )
    except (OverflowError, OSError, ValueError) as error:
        raise NormalizationError() from error


def _decode_full_packet(
    packet: bytes,
    reference: DhanInstrumentReference,
) -> tuple[MarketData, ...]:
    expected_length = _HEADER.size + _FULL_PAYLOAD.size + 5 * _DEPTH_LEVEL.size
    if len(packet) != expected_length:
        raise NormalizationError()
    try:
        (
            last_price,
            last_trade_quantity,
            last_trade_time,
            average_trade_price,
            volume,
            total_sell_quantity,
            total_buy_quantity,
            open_interest,
            highest_open_interest,
            lowest_open_interest,
            day_open,
            day_close,
            day_high,
            day_low,
        ) = _FULL_PAYLOAD.unpack_from(packet, _HEADER.size)
        _require_positive_finite(last_price)
        _require_nonnegative_integral(
            last_trade_quantity,
            volume,
            total_sell_quantity,
            total_buy_quantity,
            open_interest,
            highest_open_interest,
            lowest_open_interest,
        )
        _require_nonnegative_finite(average_trade_price, day_open, day_close, day_high, day_low)
        event_timestamp = _epoch_timestamp(last_trade_time)
        bids: list[DepthLevel] = []
        asks: list[DepthLevel] = []
        for offset in range(_HEADER.size + _FULL_PAYLOAD.size, len(packet), _DEPTH_LEVEL.size):
            (
                bid_quantity,
                ask_quantity,
                bid_order_count,
                ask_order_count,
                bid_price,
                ask_price,
            ) = _DEPTH_LEVEL.unpack_from(packet, offset)
            _require_nonnegative_integral(
                bid_quantity,
                ask_quantity,
                bid_order_count,
                ask_order_count,
            )
            _require_positive_finite(bid_price)
            _require_positive_finite(ask_price)
            bids.append(DepthLevel(price=Decimal(str(bid_price)), quantity=bid_quantity))
            asks.append(DepthLevel(price=Decimal(str(ask_price)), quantity=ask_quantity))
        tick = Tick(
            instrument=reference.instrument,
            event_timestamp=event_timestamp,
            last_price=Decimal(str(last_price)),
            traded_quantity=last_trade_quantity,
            session_cumulative_volume=volume,
            session_ohlc=_session_ohlc(day_open, day_high, day_low, day_close),
        )
        quote = Quote(
            instrument=reference.instrument,
            event_timestamp=event_timestamp,
            bid_price=bids[0].price,
            ask_price=asks[0].price,
            bid_quantity=bids[0].quantity,
            ask_quantity=asks[0].quantity,
        )
        depth = DepthSnapshot(
            instrument=reference.instrument,
            event_timestamp=event_timestamp,
            bids=tuple(bids),
            asks=tuple(asks),
        )
        return (tick, quote, depth)
    except (OverflowError, OSError, ValueError) as error:
        raise NormalizationError() from error


def _decode_previous_close_packet(packet: bytes) -> tuple[MarketData, ...]:
    """Validate Dhan's auxiliary packet without inventing a canonical live event."""
    if len(packet) != _HEADER.size + _PREVIOUS_CLOSE_PAYLOAD.size:
        raise NormalizationError()
    try:
        previous_close, open_interest = _PREVIOUS_CLOSE_PAYLOAD.unpack_from(packet, _HEADER.size)
        _require_nonnegative_finite(previous_close)
        _require_nonnegative_integral(open_interest)
    except ValueError as error:
        raise NormalizationError() from error
    return ()


def _decode_feed_disconnect_packet(packet: bytes) -> None:
    """Validate Dhan's disconnect payload without exposing its provider-only reason code."""
    if len(packet) != _HEADER.size + _FEED_DISCONNECT_PAYLOAD.size:
        raise NormalizationError()
    _FEED_DISCONNECT_PAYLOAD.unpack_from(packet, _HEADER.size)


def _resolve_reference(
    cash_references: tuple[DhanInstrumentReference, ...],
    exchange_segment_code: int,
    security_id: int,
) -> DhanInstrumentReference:
    for reference in cash_references:
        exchange_segment = reference.exchange_segment
        if exchange_segment is None:
            continue
        expected_exchange_segment = _EXCHANGE_SEGMENT_CODES.get(exchange_segment)
        try:
            expected_security_id = int(reference.security_id)
        except ValueError as error:
            raise NormalizationError() from error
        if (
            expected_exchange_segment == exchange_segment_code
            and expected_security_id == security_id
        ):
            return reference
    raise UnknownProviderReferenceError()


def _session_ohlc(
    day_open: float, day_high: float, day_low: float, day_close: float
) -> ProviderSessionOhlc | None:
    """Map the provider day-OHLC aggregate to the canonical value, or None if unavailable.

    A provider may report zero/uninitialised OHLC before a valid session aggregate
    exists; such values cannot satisfy the canonical :class:`ProviderSessionOhlc`
    contract and are surfaced as an absent aggregate (fail-closed). Nothing is ever
    fabricated from other facts — no last price, previous close, or observed extremum
    is substituted (ADR-008). Decimal conversion follows the same ``Decimal(str(...))``
    convention as the tick's other prices.
    """
    try:
        return ProviderSessionOhlc(
            open_price=Decimal(str(day_open)),
            high_price=Decimal(str(day_high)),
            low_price=Decimal(str(day_low)),
            close_price=Decimal(str(day_close)),
        )
    except ValidationError:
        return None


def _require_positive_finite(value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError()


def _require_nonnegative_finite(*values: float) -> None:
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError()


def _require_nonnegative_integral(*values: int) -> None:
    if any(value < 0 for value in values):
        raise ValueError()


def _epoch_timestamp(value: int) -> datetime:
    if value <= 0:
        raise ValueError()
    return datetime.fromtimestamp(value, tz=UTC)


def _feed_mode_for(request: SubscriptionRequest) -> DhanLiveFeedMode:
    if request.data_types == frozenset({MarketDataKind.TICK}):
        return DhanLiveFeedMode.QUOTE
    if request.data_types.issubset(
        frozenset({MarketDataKind.TICK, MarketDataKind.QUOTE, MarketDataKind.DEPTH})
    ):
        return DhanLiveFeedMode.FULL
    raise UnsupportedProviderRequestError()


def _reference_sort_key(reference: DhanInstrumentReference) -> tuple[str, str, str]:
    """Order provider requests independently of consumer request ordering."""
    instrument = reference.instrument
    return (instrument.exchange, instrument.symbol, reference.security_id)
