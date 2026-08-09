"""Pure reconciliation matching: exact identity and outcomes (P4.5D; §41)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.market_engine.context import CandleQuality, IncompleteCandle
from app.market_engine.historical.reconciliation import (
    ReconciliationOutcome,
    identity_of,
    identity_of_incomplete,
    match_outcome,
)
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, Instrument

_FIVE = Timeframe.minutes(5)
_BASE = datetime(2026, 8, 6, 3, 45, tzinfo=UTC)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _candle(
    start_min: int = 0,
    end_min: int = 5,
    *,
    symbol: str = "RELIANCE",
    high: str = "101",
    quantity: int = 10,
) -> Candle:
    return Candle(
        instrument=_instrument(symbol),
        start_timestamp=_BASE + timedelta(minutes=start_min),
        end_timestamp=_BASE + timedelta(minutes=end_min),
        open_price=Decimal("100"),
        high_price=Decimal(high),
        low_price=Decimal("99"),
        close_price=Decimal("100"),
        traded_quantity=quantity,
    )


def _incomplete(
    start_min: int = 0,
    end_min: int = 5,
    *,
    symbol: str = "RELIANCE",
    timeframe: Timeframe = _FIVE,
    high: str = "105",
) -> IncompleteCandle:
    return IncompleteCandle(
        instrument=_instrument(symbol),
        timeframe=timeframe,
        start_timestamp=_BASE + timedelta(minutes=start_min),
        end_timestamp=_BASE + timedelta(minutes=end_min),
        open_price=Decimal("100"),
        high_price=Decimal(high),
        low_price=Decimal("99"),
        close_price=Decimal("100"),
        traded_quantity=7,
        quality=CandleQuality.INCOMPLETE_VOLUME,
    )


def _match(authoritative: Candle, incomplete=(), finalized=()) -> ReconciliationOutcome:  # noqa: ANN001
    return match_outcome(
        authoritative=authoritative, timeframe=_FIVE, incomplete=incomplete, finalized=finalized
    )


def test_exact_match_reconciles() -> None:
    assert _match(_candle(), incomplete=[_incomplete()]) is ReconciliationOutcome.RECONCILED


def test_no_matching_identity_is_no_match() -> None:
    assert _match(_candle(), incomplete=[_incomplete(5, 10)]) is ReconciliationOutcome.NO_MATCH


def test_already_reconciled_when_finalized_equal() -> None:
    candle = _candle()
    assert _match(candle, finalized=[candle]) is ReconciliationOutcome.ALREADY_RECONCILED


def test_conflict_when_finalized_differs() -> None:
    assert _match(_candle(high="101"), finalized=[_candle(high="106")]) is (
        ReconciliationOutcome.CONFLICT
    )


def test_different_instrument_is_no_match() -> None:
    assert _match(_candle(symbol="TCS"), incomplete=[_incomplete()]) is (
        ReconciliationOutcome.NO_MATCH
    )


def test_different_timeframe_is_no_match() -> None:
    seven = _incomplete(timeframe=Timeframe.minutes(7))
    assert _match(_candle(), incomplete=[seven]) is ReconciliationOutcome.NO_MATCH


def test_different_start_end_is_no_match() -> None:
    assert _match(_candle(0, 5), incomplete=[_incomplete(0, 7)]) is ReconciliationOutcome.NO_MATCH


def test_provisional_ohlc_disagreement_still_reconciles() -> None:
    # Live incomplete high=105, authoritative high=106 — identity is time-based only.
    assert _match(_candle(high="106"), incomplete=[_incomplete(high="105")]) is (
        ReconciliationOutcome.RECONCILED
    )


def test_original_incomplete_is_immutable() -> None:
    incomplete = _incomplete()
    with pytest.raises(ValidationError):
        incomplete.high_price = Decimal("200")  # type: ignore[misc]


def test_identity_helpers_agree_on_matching_interval() -> None:
    assert identity_of(_candle(), _FIVE) == identity_of_incomplete(_incomplete())
