"""Bounded live-frame decode diagnostics (SECTOR-VIEW-1D root-cause instrumentation)."""

from __future__ import annotations

import struct

from app.adapters.base.errors import (
    NormalizationError,
    UnknownProviderReferenceError,
    UnsupportedProviderRequestError,
)
from app.adapters.dhan.adapter import _DECODE_FAILURE_KINDS
from app.adapters.dhan.live import DhanLiveDecodeCounters

_HEADER = struct.Struct("<B h B i")
_PREV_CLOSE_CODE = 6
_QUOTE_CODE = 4


def _frame(response_code: int, *, security_id: int = 101, extra: int = 8) -> bytes:
    """Craft a header-shaped frame with the given response code and total length."""
    length = _HEADER.size + extra
    return _HEADER.pack(response_code, length, 1, security_id) + b"\x00" * extra


def test_decoded_frame_counts_by_response_code() -> None:
    counters = DhanLiveDecodeCounters()
    counters.record(_frame(_QUOTE_CODE), kind="decoded")
    snap = counters.snapshot()
    assert snap.frames_total == 1
    assert snap.decoded_total == 1
    assert snap.failures_total == 0
    assert snap.frames_by_response_code == {_QUOTE_CODE: 1}


def test_failure_kinds_are_attributed() -> None:
    counters = DhanLiveDecodeCounters()
    counters.record(_frame(_QUOTE_CODE), kind="normalization")
    counters.record(_frame(99), kind="unsupported_code")
    counters.record(_frame(_QUOTE_CODE), kind="unknown_reference")
    snap = counters.snapshot()
    assert snap.normalization_failures == 1
    assert snap.unsupported_code_failures == 1
    assert snap.unknown_reference_failures == 1
    assert snap.failures_total == 3
    assert snap.failures_by_response_code == {_QUOTE_CODE: 2, 99: 1}


def test_code6_seen_and_failed_answers_discard_question() -> None:
    counters = DhanLiveDecodeCounters()
    counters.record(_frame(_PREV_CLOSE_CODE), kind="decoded")  # a code-6 that decoded fine
    counters.record(_frame(_PREV_CLOSE_CODE), kind="unknown_reference")  # a code-6 discarded
    snap = counters.snapshot()
    assert snap.previous_close_frames_seen == 2
    assert snap.previous_close_frames_failed == 1  # exactly the discarded one


def test_non_binary_frame_counted() -> None:
    counters = DhanLiveDecodeCounters()
    counters.record_non_binary()
    snap = counters.snapshot()
    assert snap.non_binary_frames == 1
    assert snap.failures_total == 1
    assert snap.frames_total == 1


def test_length_buckets_are_bounded() -> None:
    counters = DhanLiveDecodeCounters()
    for extra in range(100):  # 100 distinct lengths, cap is 32 -> overflow to "other"
        counters.record(_frame(_QUOTE_CODE, extra=extra), kind="normalization")
    snap = counters.snapshot()
    assert len(snap.failures_by_length_bucket) <= 33  # 32 distinct + "other"
    assert "other" in snap.failures_by_length_bucket


def test_truncated_frame_has_no_response_code() -> None:
    counters = DhanLiveDecodeCounters()
    counters.record(b"\x01\x02", kind="normalization")  # shorter than header
    snap = counters.snapshot()
    assert snap.frames_total == 1
    assert snap.normalization_failures == 1
    assert snap.frames_by_response_code == {}  # no code peekable


def test_decode_failure_kind_mapping_covers_boundary_errors() -> None:
    assert _DECODE_FAILURE_KINDS[NormalizationError] == "normalization"
    assert _DECODE_FAILURE_KINDS[UnsupportedProviderRequestError] == "unsupported_code"
    assert _DECODE_FAILURE_KINDS[UnknownProviderReferenceError] == "unknown_reference"
