"""WebSocket multi-packet framing (MARKET-DATA-LIVE-REMEDIATION-1).

Dhan stacks multiple provider packets in one WebSocket binary message (DhanHQ v2 docs:
"break down the packet on the basis of length"). These tests prove the framing splitter walks
the buffer by the header's message_length, decodes each stacked packet (incl. code-6 after
offset 0), and fails closed on truncation without discarding earlier valid packets.
"""

from __future__ import annotations

import struct
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app.adapters.base.errors import NormalizationError
from app.adapters.dhan.adapter import DhanRestAdapter
from app.adapters.dhan.live import decode_standard_live_packet, iter_standard_live_packets
from app.schemas.market_data import MarketReference, Tick

_HEADER = struct.Struct("<B h B i")
_FIXTURES = Path(__file__).parents[1] / "fixtures" / "dhan"


def _crafted(response_code: int, message_length: int, *, security_id: int = 101) -> bytes:
    """A header-shaped packet of the given total length (header + zero payload)."""
    header = _HEADER.pack(response_code, message_length, 1, security_id)
    return header + b"\x00" * max(0, message_length - _HEADER.size)


def _lengths(message: bytes) -> list[int]:
    return [len(p) for p in iter_standard_live_packets(message)]


# --------------------------------------------------------------------------- #
# Framing splitter — happy path
# --------------------------------------------------------------------------- #
def test_single_packet() -> None:
    assert _lengths(_crafted(4, 50)) == [50]


def test_two_packets() -> None:
    assert _lengths(_crafted(4, 50) + _crafted(4, 50)) == [50, 50]


def test_three_packets() -> None:
    assert _lengths(_crafted(4, 50) + _crafted(4, 50) + _crafted(4, 50)) == [50, 50, 50]


def test_mixed_quote_then_previous_close() -> None:
    packets = list(iter_standard_live_packets(_crafted(4, 50) + _crafted(6, 16)))
    assert [len(p) for p in packets] == [50, 16]
    assert packets[1][0] == 6  # code-6 sits at offset 50 and is delimited


def test_empty_message_yields_nothing() -> None:
    assert _lengths(b"") == []


def test_exact_buffer_consumption_and_order() -> None:
    message = _crafted(4, 50, security_id=1) + _crafted(4, 50, security_id=2)
    packets = list(iter_standard_live_packets(message))
    assert sum(len(p) for p in packets) == len(message)
    assert [_HEADER.unpack_from(p)[3] for p in packets] == [1, 2]  # wire order preserved


# --------------------------------------------------------------------------- #
# Framing splitter — malformed / truncation (fail closed)
# --------------------------------------------------------------------------- #
def test_shorter_than_header_raises() -> None:
    with pytest.raises(NormalizationError):
        _lengths(b"\x04\x02")


def test_message_length_zero_raises() -> None:
    with pytest.raises(NormalizationError):
        _lengths(_HEADER.pack(4, 0, 1, 101))


def test_message_length_below_header_raises() -> None:
    with pytest.raises(NormalizationError):
        _lengths(_HEADER.pack(4, 5, 1, 101) + b"\x00" * 20)


def test_packet_length_beyond_buffer_raises() -> None:
    with pytest.raises(NormalizationError):
        _lengths(_crafted(4, 50)[:30])  # header claims 50, only 30 present


def test_valid_first_then_truncated_second_keeps_first() -> None:
    message = _crafted(4, 50) + _crafted(4, 50)[:20]  # second packet truncated
    yielded: list[bytes] = []
    with pytest.raises(NormalizationError):
        for packet in iter_standard_live_packets(message):
            yielded.append(packet)
    assert len(yielded) == 1  # the first valid packet was yielded before the boundary error


# --------------------------------------------------------------------------- #
# Integration: framing + decode of real fixtures — code-6 after offset 0 decodes
# --------------------------------------------------------------------------- #
def _cash_references() -> tuple[Any, ...]:
    dhan = import_module("app.adapters.dhan")
    refs = dhan.normalize_instrument_master(
        (_FIXTURES / "instrument_master_production_universe.csv").read_text(encoding="utf-8")
    )
    return dhan.resolve_nse_cash_equity_live_universe(refs).cash_references


def test_stacked_quote_and_previous_close_both_decode() -> None:
    refs = _cash_references()
    quote = bytes.fromhex((_FIXTURES / "live_quote_packet.hex").read_text().strip())
    prev_close = bytes.fromhex((_FIXTURES / "live_previous_close_packet.hex").read_text().strip())
    message = quote + prev_close  # one WS message, two stacked packets (66 bytes)

    events: list[object] = []
    for packet in iter_standard_live_packets(message):
        events.extend(decode_standard_live_packet(packet, refs))

    kinds = [type(e) for e in events]
    assert Tick in kinds
    assert MarketReference in kinds  # code-6 delivered at offset 50 is now decoded, not discarded


# --------------------------------------------------------------------------- #
# Adapter path: later malformed packet never discards earlier valid packets
# --------------------------------------------------------------------------- #
def _adapter_with_refs() -> DhanRestAdapter:
    adapter = DhanRestAdapter(access_token=SecretStr("token"), live_client_id=SecretStr("client"))
    adapter._live_cash_references = _cash_references()
    return adapter


def test_adapter_keeps_valid_packet_despite_trailing_garbage() -> None:
    adapter = _adapter_with_refs()
    quote = bytes.fromhex((_FIXTURES / "live_quote_packet.hex").read_text().strip())
    message = quote + b"\x04\x99"  # valid quote + undelimitable trailing bytes
    events = adapter._decode_live_frame(message)
    assert events is not None and len(events) == 1  # the quote survived
    diag = adapter.live_feed_decode_diagnostics()
    assert diag.decoded_total == 1
    assert diag.framing_failures == 1
    assert diag.websocket_messages_total == 1


def test_adapter_decodes_stacked_message_into_multiple_events() -> None:
    adapter = _adapter_with_refs()
    quote = bytes.fromhex((_FIXTURES / "live_quote_packet.hex").read_text().strip())
    prev_close = bytes.fromhex((_FIXTURES / "live_previous_close_packet.hex").read_text().strip())
    events = adapter._decode_live_frame(quote + prev_close)
    assert events is not None and len(events) == 2
    diag = adapter.live_feed_decode_diagnostics()
    assert diag.frames_total == 2  # two provider packets from one WS message
    assert diag.websocket_messages_total == 1
    assert diag.previous_close_frames_seen == 1
