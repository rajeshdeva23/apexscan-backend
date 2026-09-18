"""Gate-A raw-LTT diagnostic tests (FIX-2 / RC3) — evidence-only, must not alter behavior.

Proves the diagnostic is default-OFF, captures the raw LTT integer BEFORE conversion, is bounded /
rate-limited / instrument-scoped, leaks no secret or packet bytes, computes the SAME conversion math
as the live path, and never changes decode or future-timestamp validation. It also fixes a future
LTT and confirms the record flags REJECT while the real validator's rejection is unchanged.
"""

from __future__ import annotations

import logging
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import SecretStr

from app.adapters.dhan import (
    DhanRestAdapter,
    normalize_instrument_master,
    resolve_nse_cash_equity_live_universe,
)
from app.adapters.dhan import live as dhan_live
from app.adapters.dhan.live import decode_standard_live_packet
from app.adapters.dhan.ltt_diagnostics import (
    RawLttDiagnosticConfig,
    RawLttDiagnosticRecorder,
    peek_ltt_fields,
)
from app.market_engine.validation import ValidationOutcome, classify
from app.schemas.market_data import Tick

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "dhan"
_LTT_OFFSET = 14  # header(8) + last_price f(4) + last_trade_quantity h(2)
_NOW = datetime(2026, 9, 18, 4, 0, 0, tzinfo=UTC)


def _fixture_packet() -> bytearray:
    text = (_FIXTURES / "live_quote_packet.hex").read_text(encoding="utf-8").strip()
    return bytearray(bytes.fromhex(text))


def _universe():  # noqa: ANN202 - test helper
    refs = normalize_instrument_master(
        (_FIXTURES / "instrument_master_production_universe.csv").read_text(encoding="utf-8")
    )
    return resolve_nse_cash_equity_live_universe(refs)


def _with_ltt(packet: bytearray, ltt: int) -> bytes:
    copy = bytearray(packet)
    struct.pack_into("<i", copy, _LTT_OFFSET, ltt)
    return bytes(copy)


def _enabled(**over: object) -> RawLttDiagnosticRecorder:
    over.setdefault("min_interval_seconds", 0.0)
    return RawLttDiagnosticRecorder(RawLttDiagnosticConfig(enabled=True, **over))


# --- default OFF -------------------------------------------------------------------------------


def test_default_recorder_is_off_and_emits_nothing(caplog) -> None:
    recorder = RawLttDiagnosticRecorder()  # default config: disabled
    with caplog.at_level(logging.INFO):
        assert recorder.observe(bytes(_fixture_packet()), receive_utc=_NOW) is None
    assert recorder.emitted == 0
    assert "RAW_LTT_DIAGNOSTIC" not in caplog.text


# --- captures raw integer before conversion ----------------------------------------------------


def test_enabled_captures_raw_ltt_integer() -> None:
    packet = _with_ltt(_fixture_packet(), 1_789_000_000)
    sample = _enabled().observe(packet, receive_utc=_NOW)
    assert sample is not None
    assert sample.raw_ltt == 1_789_000_000  # the exact wire integer, not a converted value
    assert sample.decoded_utc == datetime.fromtimestamp(1_789_000_000, tz=UTC)


def test_conversion_math_matches_the_live_path() -> None:
    # The diagnostic must decode-for-evidence identically to live._epoch_timestamp (no offset).
    raw = 1_789_012_345
    sample = _enabled().observe(_with_ltt(_fixture_packet(), raw), receive_utc=_NOW)
    assert sample is not None
    assert sample.decoded_utc == dhan_live._epoch_timestamp(raw)


def test_peek_reads_same_field_the_decoder_uses() -> None:
    universe = _universe()
    packet = bytes(_fixture_packet())
    events = decode_standard_live_packet(packet, universe.cash_references)
    tick = next(event for event in events if isinstance(event, Tick))
    fields = peek_ltt_fields(packet)
    assert fields is not None
    _rc, _seg, _sec, raw_ltt = fields
    assert tick.event_timestamp == datetime.fromtimestamp(raw_ltt, tz=UTC)


# --- bounds: sample cap, rate limit, allow-list ------------------------------------------------


def test_sample_cap_is_enforced() -> None:
    recorder = _enabled(max_samples=2)
    packet = _with_ltt(_fixture_packet(), 1_789_000_000)
    assert recorder.observe(packet, receive_utc=_NOW) is not None
    assert recorder.observe(packet, receive_utc=_NOW) is not None
    assert recorder.observe(packet, receive_utc=_NOW) is None  # capped
    assert recorder.emitted == 2


def test_rate_limit_drops_samples_inside_the_interval() -> None:
    recorder = _enabled(min_interval_seconds=5.0)
    packet = _with_ltt(_fixture_packet(), 1_789_000_000)
    assert recorder.observe(packet, receive_utc=_NOW) is not None
    assert recorder.observe(packet, receive_utc=_NOW + timedelta(seconds=1)) is None
    assert recorder.observe(packet, receive_utc=_NOW + timedelta(seconds=6)) is not None
    assert recorder.emitted == 2


def test_instrument_allow_list_scopes_capture() -> None:
    packet = _with_ltt(_fixture_packet(), 1_789_000_000)
    security_id = peek_ltt_fields(packet)[2]
    included = _enabled(allowed_security_ids=frozenset({security_id}))
    excluded = _enabled(allowed_security_ids=frozenset({security_id + 1}))
    assert included.observe(packet, receive_utc=_NOW) is not None
    assert excluded.observe(packet, receive_utc=_NOW) is None


# --- secret safety -----------------------------------------------------------------------------


def test_log_line_has_no_secret_or_packet_bytes(caplog) -> None:
    packet = _with_ltt(_fixture_packet(), 1_789_000_000)
    with caplog.at_level(logging.INFO):
        _enabled().observe(packet, receive_utc=_NOW)
    text = caplog.text
    assert "RAW_LTT_DIAGNOSTIC" in text
    assert packet.hex() not in text  # never the raw payload bytes
    for secret in ("token", "secret", "password", "client_id", "authorization"):
        assert secret not in text.lower()


# --- non-LTT packets are ignored ---------------------------------------------------------------


def test_non_ltt_response_code_is_ignored() -> None:
    packet = _fixture_packet()
    packet[0] = 6  # previous-close response code — carries no LTT
    assert peek_ltt_fields(bytes(packet)) is None
    assert _enabled().observe(bytes(packet), receive_utc=_NOW) is None


def test_nonpositive_raw_ltt_records_evidence_but_marks_reject() -> None:
    sample = _enabled().observe(_with_ltt(_fixture_packet(), 0), receive_utc=_NOW)
    assert sample is not None
    assert sample.raw_ltt == 0
    assert sample.decoded_utc is None
    assert sample.future_validation == "REJECT"


# --- future timestamp: recorded as REJECT; real validator behavior unchanged -------------------


def test_future_ltt_flagged_reject_and_validator_still_rejects() -> None:
    future_epoch = int(_NOW.timestamp()) + 19_800  # IST-naive-shaped shift (+5:30)
    sample = _enabled().observe(_with_ltt(_fixture_packet(), future_epoch), receive_utc=_NOW)
    assert sample is not None
    assert sample.future_validation == "REJECT"
    assert sample.delta_seconds is not None and sample.delta_seconds > 60
    # The real validator (untouched by the diagnostic) still rejects a future tick and accepts now.
    universe = _universe()
    instrument = universe.cash_references[0].instrument
    future_tick = _tick(instrument, _NOW + timedelta(seconds=19_800))
    now_tick = _tick(instrument, _NOW)
    assert classify(future_tick, known=True, state=None, now=_NOW) is ValidationOutcome.INVALID
    assert classify(now_tick, known=True, state=None, now=_NOW) is ValidationOutcome.ACCEPT


# --- adapter integration: decode identical with diagnostic ON vs OFF ---------------------------


def _decode_via_adapter(recorder: RawLttDiagnosticRecorder | None):  # noqa: ANN202
    adapter = DhanRestAdapter(access_token=SecretStr("fixture-token"), ltt_diagnostic=recorder)
    adapter._live_cash_references = _universe().cash_references
    return adapter._decode_one_packet(bytes(_fixture_packet()))


def test_adapter_decode_output_identical_on_vs_off() -> None:
    off_events = _decode_via_adapter(None)  # default disabled recorder
    recorder = _enabled()
    on_events = _decode_via_adapter(recorder)
    assert on_events == off_events  # diagnostic never changes the decoded events
    assert recorder.emitted == 1  # but it did observe once when enabled


def test_header_offset_drift_guard() -> None:
    # If the wire layout changes upstream, this pins the LTT offset the diagnostic reads.
    from app.adapters.dhan import ltt_diagnostics as diag

    assert diag._HEADER.size == 8
    assert diag._LTT_OFFSET == _LTT_OFFSET


# --- per-security sampling (Gate-A two-instrument fairness, GATE-A-PER-SEC) --------------------

_SEC_A = 1001
_SEC_B = 2002
_SECID_OFFSET = 4  # header "<B h B i": security_id int32 at byte offset 4


def _with_security_id(packet: bytearray, security_id: int) -> bytes:
    copy = bytearray(packet)
    struct.pack_into("<i", copy, _SECID_OFFSET, security_id)
    return bytes(copy)


def _pkt(security_id: int, ltt: int = 1_789_000_000) -> bytes:
    return _with_security_id(_with_ltt(_fixture_packet(), ltt), security_id)


def _per_security(**over: object) -> RawLttDiagnosticRecorder:
    over.setdefault("min_interval_seconds", 90.0)
    over.setdefault("max_samples", 300)
    over.setdefault("allowed_security_ids", frozenset({_SEC_A, _SEC_B}))
    return RawLttDiagnosticRecorder(RawLttDiagnosticConfig(enabled=True, **over))


def test_per_security_each_instrument_emits_independently() -> None:
    # Same instant: with the old global interval B would be blocked by A. Per-security, both emit.
    recorder = _per_security()
    sample_a = recorder.observe(_pkt(_SEC_A), receive_utc=_NOW)
    sample_b = recorder.observe(_pkt(_SEC_B), receive_utc=_NOW)
    assert sample_a is not None and sample_a.security_id == _SEC_A
    assert sample_b is not None and sample_b.security_id == _SEC_B
    assert recorder.emitted == 2


def test_per_security_b_emits_while_a_is_rate_limited() -> None:
    recorder = _per_security(min_interval_seconds=90.0)
    assert recorder.observe(_pkt(_SEC_A), receive_utc=_NOW) is not None
    # A is inside its 90s window; B (untouched) still emits at the same instant.
    assert recorder.observe(_pkt(_SEC_A), receive_utc=_NOW + timedelta(seconds=10)) is None
    assert recorder.observe(_pkt(_SEC_B), receive_utc=_NOW + timedelta(seconds=10)) is not None


def test_per_security_a_emits_while_b_is_rate_limited() -> None:
    recorder = _per_security(min_interval_seconds=90.0)
    assert recorder.observe(_pkt(_SEC_B), receive_utc=_NOW) is not None
    assert recorder.observe(_pkt(_SEC_B), receive_utc=_NOW + timedelta(seconds=10)) is None
    assert recorder.observe(_pkt(_SEC_A), receive_utc=_NOW + timedelta(seconds=10)) is not None


def test_per_security_interval_is_independent_per_instrument() -> None:
    recorder = _per_security(min_interval_seconds=90.0)
    assert recorder.observe(_pkt(_SEC_A), receive_utc=_NOW) is not None
    assert recorder.observe(_pkt(_SEC_A), receive_utc=_NOW + timedelta(seconds=30)) is None
    assert recorder.observe(_pkt(_SEC_A), receive_utc=_NOW + timedelta(seconds=90)) is not None


def test_per_security_quota_is_independent_and_total_bound_is_deterministic() -> None:
    recorder = _per_security(max_samples=2, min_interval_seconds=0.0)
    assert recorder.observe(_pkt(_SEC_A), receive_utc=_NOW) is not None
    assert recorder.observe(_pkt(_SEC_A), receive_utc=_NOW) is not None
    assert recorder.observe(_pkt(_SEC_A), receive_utc=_NOW) is None  # A exhausted its own quota
    # B's quota is untouched by A's exhaustion.
    assert recorder.observe(_pkt(_SEC_B), receive_utc=_NOW) is not None
    assert recorder.observe(_pkt(_SEC_B), receive_utc=_NOW) is not None
    assert recorder.observe(_pkt(_SEC_B), receive_utc=_NOW) is None
    assert recorder.emitted == 4  # deterministic: max_samples(2) × 2 securities


def test_max_samples_is_per_security_when_allowlisted() -> None:
    recorder = _per_security(max_samples=3, min_interval_seconds=0.0)
    for _ in range(5):
        recorder.observe(_pkt(_SEC_A), receive_utc=_NOW)
        recorder.observe(_pkt(_SEC_B), receive_utc=_NOW)
    assert recorder._emitted_by_key[_SEC_A] == 3
    assert recorder._emitted_by_key[_SEC_B] == 3
    assert recorder.emitted == 6  # 3 per security × 2 securities


def test_non_allowlisted_packet_creates_no_state() -> None:
    recorder = _per_security()
    assert recorder.observe(_pkt(9999), receive_utc=_NOW) is None
    assert recorder.emitted == 0
    assert recorder._emitted_by_key == {}  # no map entry created before the allow-list gate
    assert recorder._last_emit_by_key == {}


def test_state_key_count_cannot_exceed_allowlist_size() -> None:
    recorder = _per_security(min_interval_seconds=0.0)
    for security_id in (_SEC_A, _SEC_B, 9999, 8888, _SEC_A):
        recorder.observe(_pkt(security_id), receive_utc=_NOW)
    assert set(recorder._emitted_by_key) <= {_SEC_A, _SEC_B}
    assert len(recorder._emitted_by_key) <= len(recorder._config.allowed_security_ids)


def test_new_recorder_resets_all_per_security_counters() -> None:
    recorder = _per_security(max_samples=1, min_interval_seconds=0.0)
    assert recorder.observe(_pkt(_SEC_A), receive_utc=_NOW) is not None
    assert recorder.observe(_pkt(_SEC_A), receive_utc=_NOW) is None  # A exhausted
    fresh = _per_security(max_samples=1, min_interval_seconds=0.0)
    assert fresh.observe(_pkt(_SEC_A), receive_utc=_NOW) is not None  # restart clears the counter


def test_empty_allowlist_keeps_single_global_counter_and_interval() -> None:
    recorder = _enabled(max_samples=1, min_interval_seconds=90.0)  # empty allow-list
    assert recorder.observe(_pkt(_SEC_A), receive_utc=_NOW) is not None
    # Global cap of 1 and a shared window: a *different* instrument is still blocked.
    assert recorder.observe(_pkt(_SEC_B), receive_utc=_NOW + timedelta(seconds=10)) is None
    assert set(recorder._emitted_by_key) == {None}  # one bounded global sentinel slot


def _tick(instrument, when: datetime) -> Tick:  # noqa: ANN001 - test helper
    from decimal import Decimal

    return Tick(
        instrument=instrument,
        event_timestamp=when,
        last_price=Decimal("100.5"),
        traded_quantity=1,
        session_cumulative_volume=1,
    )
