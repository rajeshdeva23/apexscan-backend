"""Offline historical-evidence probe for SECTOR-5B prerequisites (SECTOR-5B-PRE-R3.1).

Evidence tooling only — NOT imported by application startup, the provider runtime, the
Market Engine, the EventBus, or strategies. The pure evaluators here are network-free and
deterministic. :func:`run_probe` orchestrates the *corrected* call order proven in R3.1
(``connect() -> load_instruments() -> resolve -> load_historical_data``) and is exercised in
tests against a mock adapter — this module performs no Dhan authentication or network I/O by
itself. Nothing here decides thresholds, scores, or classifications (that is SECTOR-5C/5D).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app.schemas.market_data import (
    Candle,
    HistoricalRequest,
    HistoricalResult,
    Instrument,
    InstrumentClass,
    MarketSegment,
)

IST = timezone(timedelta(hours=5, minutes=30))

_SECRET_HINTS = ("token", "pin", "totp", "secret", "authorization", "cookie", "password")


class BarLabel(StrEnum):
    """Empirically-derived provider bar-labelling verdict (no assumption baked in)."""

    LEFT_LABEL = "left_label"
    RIGHT_LABEL = "right_label"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"


def classify_bar_label(
    *,
    first_start_utc: datetime,
    last_end_utc: datetime,
    bar_count: int,
    session_open_ist: datetime,
    session_close_ist: datetime,
    interval: timedelta = timedelta(minutes=1),
) -> BarLabel:
    """Classify provider labelling from corroborating evidence, never a single timestamp.

    LEFT_LABEL requires the first bar's start to equal the session open AND the last bar's
    end to equal the session close. RIGHT_LABEL requires both shifted by one interval.
    Anything else is AMBIGUOUS (§12: a lone 09:15 does not prove semantics).
    """
    if bar_count <= 0:
        return BarLabel.INVALID
    first_ist = first_start_utc.astimezone(IST)
    last_ist = last_end_utc.astimezone(IST)
    if first_ist == session_open_ist and last_ist == session_close_ist:
        return BarLabel.LEFT_LABEL
    if first_ist == session_open_ist + interval and last_ist == session_close_ist + interval:
        return BarLabel.RIGHT_LABEL
    return BarLabel.AMBIGUOUS


def feature_eligible(candle_end_utc: datetime, evaluation_time_utc: datetime) -> bool:
    """Anti-lookahead rule: a candle is feature-eligible iff it closed at/before T."""
    return candle_end_utc <= evaluation_time_utc


def resolve_instrument(
    instruments: Sequence[Instrument], symbol: str, exchange: str = "NSE"
) -> Instrument | None:
    """Resolve exactly one cash-equity Instrument from ``load_instruments()`` output.

    Returns None unless exactly one match exists (fail-closed; no fuzzy/security-id guess).
    """
    matches = [
        i
        for i in instruments
        if i.symbol == symbol
        and i.exchange == exchange
        and i.market_segment is MarketSegment.EQUITY
        and i.instrument_class is InstrumentClass.CASH
    ]
    return matches[0] if len(matches) == 1 else None


def expected_minute_starts(
    session_start_utc: datetime,
    session_end_utc: datetime,
    interval: timedelta = timedelta(minutes=1),
) -> list[datetime]:
    """Return the expected half-open [start, end) bar-start grid for a session."""
    out: list[datetime] = []
    cursor = session_start_utc
    while cursor < session_end_utc:
        out.append(cursor)
        cursor += interval
    return out


@dataclass(frozen=True)
class SessionQuality:
    """Deterministic completeness/validity summary for one session's candles."""

    expected: int
    returned: int
    missing: tuple[datetime, ...]
    duplicates: int
    invalid_ohlc: int
    out_of_session: int
    non_monotonic: int


def session_quality(
    candles: Sequence[Candle],
    expected_starts_utc: Sequence[datetime],
    session_start_utc: datetime,
    session_end_utc: datetime,
) -> SessionQuality:
    """Compute missing/duplicate/invalid/out-of-session/non-monotonic — no fill or synthesis."""
    starts = [c.start_timestamp for c in candles]
    returned = set(starts)
    expected = set(expected_starts_utc)
    invalid = sum(
        1
        for c in candles
        if not (
            c.low_price > 0
            and c.low_price <= c.open_price <= c.high_price
            and c.low_price <= c.close_price <= c.high_price
        )
    )
    out_of_session = sum(1 for s in starts if not (session_start_utc <= s < session_end_utc))
    non_monotonic = sum(1 for a, b in zip(starts, starts[1:], strict=False) if b <= a)
    return SessionQuality(
        expected=len(expected),
        returned=len(candles),
        missing=tuple(sorted(expected - returned)),
        duplicates=len(starts) - len(returned),
        invalid_ohlc=invalid,
        out_of_session=out_of_session,
        non_monotonic=non_monotonic,
    )


def write_raw_artifact(path: Path, candles: Sequence[Candle], request_meta: dict[str, str]) -> str:
    """Write a sanitized raw artifact (start-epochs + OHLC + meta) and return its SHA-256.

    Rejects any request_meta key that looks credential-bearing (fail-closed; never persist
    tokens/PIN/TOTP/Authorization/cookies). Large raw files belong outside Git.
    """
    for key in request_meta:
        if any(hint in key.lower() for hint in _SECRET_HINTS):
            raise ValueError(f"refusing to write credential-like field: {key}")
    payload = json.dumps(
        {
            "meta": request_meta,
            "candles": [
                [
                    int(c.start_timestamp.timestamp()),
                    str(c.open_price),
                    str(c.high_price),
                    str(c.low_price),
                    str(c.close_price),
                ]
                for c in candles
            ],
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    path.write_text(payload)
    return digest


class HistoricalProbeAdapter(Protocol):
    """Minimal read-only adapter surface the probe needs (real adapter or a test mock)."""

    async def connect(self) -> None:
        """Create adapter-owned clients (no provider request)."""

    async def load_instruments(self) -> tuple[Instrument, ...]:
        """Load the instrument master and return canonical Instruments."""

    async def load_historical_data(self, request: HistoricalRequest) -> HistoricalResult:
        """Return canonical candles for the request (authenticates on first call)."""


async def run_probe(
    adapter: HistoricalProbeAdapter,
    *,
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
    interval: timedelta = timedelta(minutes=1),
) -> HistoricalResult:
    """Corrected probe flow: connect → load master → resolve → historical (one token gen).

    Fixes the R3 defect: ``load_instruments()`` runs *before* resolution so the canonical
    Instrument key exists. Profile/health is intentionally skipped (redundant — the
    historical call authenticates). Fails closed if the symbol does not resolve uniquely.
    """
    await adapter.connect()
    instruments = await adapter.load_instruments()
    instrument = resolve_instrument(instruments, symbol)
    if instrument is None:
        raise LookupError(f"{symbol} did not resolve uniquely after load_instruments()")
    request = HistoricalRequest(
        instrument=instrument, start_timestamp=start_utc, end_timestamp=end_utc, interval=interval
    )
    return await adapter.load_historical_data(request)
