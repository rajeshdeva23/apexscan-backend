"""Gate-A raw-LTT evidence diagnostic (FIX-2 / RC3) — DEFAULT OFF, observation-only.

Captures the Dhan Last-Traded-Time (LTT) **raw int32 exactly as it arrives on the wire, before
any conversion**, so the decisive Gate-A test can run: compute ``datetime.fromtimestamp(raw, UTC)``
and compare it to the packet's aware-UTC receive instant. If ``decoded ≈ receive`` the LTT is a true
POSIX epoch (ApexScan already correct); if ``decoded ≈ receive + 19800s`` the LTT is IST-naive. It
records evidence for BOTH — it never converts, corrects, or subtracts 5:30, and it never feeds the
real decode/validation path.

This is deliberately NOT a general diagnostics framework: it is one bounded, sampled, rate-limited,
optionally instrument-scoped, secret-free recorder scoped to Gate-A raw-LTT capture. It is inert
unless explicitly enabled and reads only the LTT field (never prices, credentials, or packet bytes).

Wire layout mirrors ``app.adapters.dhan.live`` (a drift test pins them equal): header ``<B h B i``
(response_code, message_length, exchange_segment_code, security_id) then the quote/full payload,
whose shared prefix ``<f h i`` puts ``last_trade_time`` (int32) at byte offset 14.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Mirrors live.py's _HEADER and the quote/full payload prefix. Only the LTT field is read.
_HEADER = struct.Struct("<B h B i")
_LTT_OFFSET = _HEADER.size + struct.Struct("<f h").size  # last_price(f) + last_trade_quantity(h)
_LTT = struct.Struct("<i")
_QUOTE_RESPONSE_CODE = 4
_FULL_RESPONSE_CODE = 8
_LTT_BEARING_CODES = frozenset({_QUOTE_RESPONSE_CODE, _FULL_RESPONSE_CODE})

# The canonical future-skew that market_engine.validation applies (docs/06 §9.1, 1 minute). Mirrored
# here only to LABEL each sample PASS/REJECT for evidence; it never gates the real validator.
_DEFAULT_MAX_FUTURE_SKEW_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class RawLttSample:
    """One sanitized raw-LTT evidence record (no prices, no credentials, no packet bytes)."""

    security_id: int
    exchange_segment_code: int
    response_code: int
    receive_utc: datetime
    raw_ltt: int
    decoded_utc: datetime | None
    delta_seconds: float | None
    future_validation: str  # "PASS" | "REJECT"


@dataclass(frozen=True, slots=True)
class RawLttDiagnosticConfig:
    """Bounds for the Gate-A recorder. Disabled and inert by default."""

    enabled: bool = False
    max_samples: int = 100
    min_interval_seconds: float = 1.0
    allowed_security_ids: frozenset[int] = field(default_factory=frozenset)
    max_future_skew_seconds: float = _DEFAULT_MAX_FUTURE_SKEW_SECONDS


def peek_ltt_fields(packet: bytes) -> tuple[int, int, int, int] | None:
    """Read (response_code, exchange_segment_code, security_id, raw_ltt) without a full decode.

    Returns ``None`` for any packet that does not carry an LTT (wrong response code, too short) —
    the recorder then samples nothing for it. Pure and side-effect-free; never raises.
    """
    if len(packet) < _LTT_OFFSET + _LTT.size:
        return None
    response_code, _message_length, exchange_segment_code, security_id = _HEADER.unpack_from(packet)
    if response_code not in _LTT_BEARING_CODES:
        return None
    (raw_ltt,) = _LTT.unpack_from(packet, _LTT_OFFSET)
    return response_code, exchange_segment_code, security_id, raw_ltt


def _decode_for_evidence(raw_ltt: int) -> datetime | None:
    """Compute ``fromtimestamp(raw, UTC)`` for the record — identical math to the live path.

    Returns ``None`` if the integer is not a representable positive epoch. This mirrors, and never
    substitutes for, ``live._epoch_timestamp``; it applies no offset and no timezone localization.
    """
    if raw_ltt <= 0:
        return None
    try:
        return datetime.fromtimestamp(raw_ltt, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


class RawLttDiagnosticRecorder:
    """Bounded, sampled, rate-limited raw-LTT evidence recorder. Inert unless enabled."""

    def __init__(self, config: RawLttDiagnosticConfig | None = None) -> None:
        self._config = config or RawLttDiagnosticConfig()
        self._emitted = 0
        self._last_emit_utc: datetime | None = None

    @property
    def emitted(self) -> int:
        """Number of evidence records emitted so far (bounded by ``max_samples``)."""
        return self._emitted

    def observe(self, packet: bytes, *, receive_utc: datetime) -> RawLttSample | None:
        """Record one raw-LTT sample if enabled and within bounds; else no-op.

        Observation-only: it reads the raw LTT integer and computes the evidence fields, but never
        mutates the packet, the decoded value, or any downstream decode/validation decision.
        Returns the emitted :class:`RawLttSample`, or ``None`` when disabled, not LTT-bearing,
        filtered out by the instrument allow-list, over the sample cap, or inside the rate window.
        """
        if not self._config.enabled or self._emitted >= self._config.max_samples:
            return None
        fields = peek_ltt_fields(packet)
        if fields is None:
            return None
        response_code, exchange_segment_code, security_id, raw_ltt = fields
        allowed = self._config.allowed_security_ids
        if allowed and security_id not in allowed:
            return None
        if self._within_rate_window(receive_utc):
            return None
        sample = self._build_sample(
            response_code, exchange_segment_code, security_id, raw_ltt, receive_utc
        )
        self._emitted += 1
        self._last_emit_utc = receive_utc
        self._emit(sample)
        return sample

    def _within_rate_window(self, receive_utc: datetime) -> bool:
        """Whether ``receive_utc`` is within the minimum interval of the last emitted sample."""
        if self._last_emit_utc is None:
            return False
        elapsed = (receive_utc - self._last_emit_utc).total_seconds()
        return elapsed < self._config.min_interval_seconds

    def _build_sample(
        self,
        response_code: int,
        exchange_segment_code: int,
        security_id: int,
        raw_ltt: int,
        receive_utc: datetime,
    ) -> RawLttSample:
        decoded_utc = _decode_for_evidence(raw_ltt)
        delta_seconds = (decoded_utc - receive_utc).total_seconds() if decoded_utc else None
        passes = delta_seconds is not None and delta_seconds <= self._config.max_future_skew_seconds
        return RawLttSample(
            security_id=security_id,
            exchange_segment_code=exchange_segment_code,
            response_code=response_code,
            receive_utc=receive_utc,
            raw_ltt=raw_ltt,
            decoded_utc=decoded_utc,
            delta_seconds=delta_seconds,
            future_validation="PASS" if passes else "REJECT",
        )

    def _emit(self, sample: RawLttSample) -> None:
        """Log one evidence line in the repository's structured style (no packet bytes/secrets)."""
        logger.info(
            "RAW_LTT_DIAGNOSTIC security_id=%s exchange_segment_code=%s response_code=%s "
            "receive_utc=%s raw_ltt=%s decoded_utc=%s delta_seconds=%s future_validation=%s",
            sample.security_id,
            sample.exchange_segment_code,
            sample.response_code,
            sample.receive_utc.isoformat(),
            sample.raw_ltt,
            sample.decoded_utc.isoformat() if sample.decoded_utc else "none",
            f"{sample.delta_seconds:.3f}" if sample.delta_seconds is not None else "none",
            sample.future_validation,
        )
