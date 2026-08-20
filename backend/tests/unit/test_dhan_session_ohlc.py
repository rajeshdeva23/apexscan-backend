"""Dhan adapter mapping of provider day-OHLC into canonical Tick.session_ohlc (P4.6A).

Builds controlled QUOTE (code 4) and FULL (code 8) packets from the adapter's own
struct layouts and asserts the already-decoded day OHLC is transported into the
canonical ``ProviderSessionOhlc`` without leaking provider field names, and that an
unavailable/invalid aggregate fails closed to ``None`` (never reconstructed).
"""

from __future__ import annotations

from decimal import Decimal

from app.adapters.dhan.live import (
    _DEPTH_LEVEL,
    _FULL_PAYLOAD,
    _FULL_RESPONSE_CODE,
    _HEADER,
    _QUOTE_PAYLOAD,
    _QUOTE_RESPONSE_CODE,
    decode_standard_live_packet,
)
from app.adapters.dhan.models import DhanInstrumentReference
from app.schemas.market_data import Instrument, ProviderSessionOhlc, Tick

_SEG_NSE_EQ = 1
_SECURITY_ID = 1
_LAST_TRADE_TIME = 1_754_000_000


def _reference() -> DhanInstrumentReference:
    return DhanInstrumentReference(
        instrument=Instrument(exchange="NSE", symbol="RELIANCE"),
        security_id=str(_SECURITY_ID),
        underlying_security_id=None,
        exchange_segment="NSE_EQ",
        provider_instrument_type="EQUITY",
    )


def _quote_packet(
    *,
    day_open: float = 100.0,
    day_close: float = 100.25,
    day_high: float = 101.5,
    day_low: float = 99.5,
    last_price: float = 100.0,
    volume: int = 1000,
    ltq: int = 5,
) -> bytes:
    payload = _QUOTE_PAYLOAD.pack(
        last_price,
        ltq,
        _LAST_TRADE_TIME,
        100.0,
        volume,
        0,
        0,
        day_open,
        day_close,
        day_high,
        day_low,
    )
    total = _HEADER.size + len(payload)
    return _HEADER.pack(_QUOTE_RESPONSE_CODE, total, _SEG_NSE_EQ, _SECURITY_ID) + payload


def _full_packet(
    *,
    day_open: float = 100.0,
    day_close: float = 100.25,
    day_high: float = 101.5,
    day_low: float = 99.5,
    last_price: float = 100.0,
    volume: int = 1000,
    ltq: int = 5,
) -> bytes:
    payload = _FULL_PAYLOAD.pack(
        last_price,
        ltq,
        _LAST_TRADE_TIME,
        100.0,
        volume,
        0,
        0,
        0,
        0,
        0,
        day_open,
        day_close,
        day_high,
        day_low,
    )
    depth = b"".join(_DEPTH_LEVEL.pack(10, 12, 1, 1, 100.0, 100.5) for _ in range(5))
    body = payload + depth
    total = _HEADER.size + len(body)
    return _HEADER.pack(_FULL_RESPONSE_CODE, total, _SEG_NSE_EQ, _SECURITY_ID) + body


def _decode(packet: bytes) -> tuple[object, ...]:
    return decode_standard_live_packet(packet, (_reference(),))


_EXPECTED = ProviderSessionOhlc(
    open_price=Decimal("100"),
    high_price=Decimal("101.5"),
    low_price=Decimal("99.5"),
    close_price=Decimal("100.25"),
)


def test_quote_packet_maps_day_ohlc_to_session_ohlc() -> None:
    tick = _decode(_quote_packet())[0]
    assert isinstance(tick, Tick)
    assert tick.session_ohlc == _EXPECTED


def test_full_packet_maps_day_ohlc_to_session_ohlc() -> None:
    tick = _decode(_full_packet())[0]
    assert isinstance(tick, Tick)
    assert tick.session_ohlc == _EXPECTED


def test_quote_and_full_produce_equivalent_session_ohlc() -> None:
    quote_tick = _decode(_quote_packet())[0]
    full_tick = _decode(_full_packet())[0]
    assert isinstance(quote_tick, Tick) and isinstance(full_tick, Tick)
    assert quote_tick.session_ohlc == full_tick.session_ohlc


def test_mapping_preserves_open_high_low_close_and_decimal_convention() -> None:
    ohlc = _decode(_quote_packet(day_open=100.0, day_high=101.5, day_low=99.5, day_close=100.25))[
        0
    ].session_ohlc
    assert ohlc is not None
    assert ohlc.open_price == Decimal("100")
    assert ohlc.high_price == Decimal("101.5")
    assert ohlc.low_price == Decimal("99.5")
    assert ohlc.close_price == Decimal("100.25")


def test_zero_sentinel_aggregate_fails_closed_to_none() -> None:
    # Provider reports an uninitialised open (0) before a valid session aggregate exists.
    tick = _decode(_quote_packet(day_open=0.0))[0]
    assert isinstance(tick, Tick)
    assert tick.session_ohlc is None
    # The tick itself remains valid and is never reconstructed from the last price.
    assert tick.last_price == Decimal("100")


def test_structurally_invalid_aggregate_fails_closed_to_none() -> None:
    # All positive, but high < low — cannot satisfy the canonical contract.
    tick = _decode(_quote_packet(day_high=99.0, day_low=100.0))[0]
    assert isinstance(tick, Tick)
    assert tick.session_ohlc is None


def test_existing_tick_fields_are_unchanged_by_the_addition() -> None:
    tick = _decode(_quote_packet(last_price=123.5, ltq=7, volume=250_000))[0]
    assert isinstance(tick, Tick)
    assert tick.last_price == Decimal("123.5")
    assert tick.traded_quantity == 7
    # ADR-005 session cumulative volume mapping is unchanged.
    assert tick.session_cumulative_volume == 250_000
