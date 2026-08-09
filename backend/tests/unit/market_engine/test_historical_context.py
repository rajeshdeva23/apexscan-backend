"""Immutable historical series / previous-session / context value types (P4.5A; §30, §31)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.market_engine.context import PartialCandle
from app.market_engine.historical.context import (
    HistoricalContext,
    HistoricalSeries,
    PreviousSessionFacts,
)
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, Instrument

_FIVE = Timeframe.minutes(5)
_FIFTEEN = Timeframe.minutes(15)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _candle(minute: int, *, symbol: str = "RELIANCE", width: int = 5) -> Candle:
    start = datetime(2026, 8, 6, 3, 45, tzinfo=UTC) + timedelta(minutes=minute)
    return Candle(
        instrument=_instrument(symbol),
        start_timestamp=start,
        end_timestamp=start + timedelta(minutes=width),
        open_price=Decimal("100"),
        high_price=Decimal("101"),
        low_price=Decimal("99"),
        close_price=Decimal("100"),
        traded_quantity=10,
    )


# --------------------------------------------------------------------------- #
# HistoricalSeries (§30)
# --------------------------------------------------------------------------- #
def test_series_is_immutable() -> None:
    series = HistoricalSeries(timeframe=_FIVE, candles=(_candle(0),))
    with pytest.raises(ValidationError):
        series.candles = ()  # type: ignore[misc]


def test_series_accepts_canonical_candles() -> None:
    series = HistoricalSeries(timeframe=_FIVE, candles=(_candle(0), _candle(5)))
    assert series.instrument == _instrument()
    assert len(series.candles) == 2


def test_empty_series_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least one candle"):
        HistoricalSeries(timeframe=_FIVE, candles=())


def test_series_with_multiple_instruments_is_rejected() -> None:
    with pytest.raises(ValidationError, match="one instrument"):
        HistoricalSeries(timeframe=_FIVE, candles=(_candle(0), _candle(5, symbol="TCS")))


def test_series_duplicate_interval_is_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate intervals"):
        HistoricalSeries(timeframe=_FIVE, candles=(_candle(0), _candle(0)))


def test_series_overlapping_intervals_are_rejected() -> None:
    with pytest.raises(ValidationError, match="overlapping intervals"):
        HistoricalSeries(timeframe=_FIVE, candles=(_candle(0, width=5), _candle(3, width=5)))


def test_series_normalizes_unordered_non_overlapping_candles() -> None:
    series = HistoricalSeries(timeframe=_FIVE, candles=(_candle(10), _candle(0), _candle(5)))
    starts = [candle.start_timestamp for candle in series.candles]
    assert starts == sorted(starts)


def test_series_rejects_partial_candle() -> None:
    partial = PartialCandle(
        start_timestamp=datetime(2026, 8, 6, 3, 45, tzinfo=UTC),
        end_timestamp=datetime(2026, 8, 6, 3, 50, tzinfo=UTC),
        open_price=Decimal("100"),
        high_price=Decimal("101"),
        low_price=Decimal("99"),
        close_price=Decimal("100"),
    )
    with pytest.raises(ValidationError):
        HistoricalSeries(timeframe=_FIVE, candles=(partial,))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# PreviousSessionFacts (§10, §11)
# --------------------------------------------------------------------------- #
def test_previous_session_facts_expose_instrument() -> None:
    facts = PreviousSessionFacts(trading_date=date(2026, 8, 5), candle=_candle(0))
    assert facts.instrument == _instrument()
    assert facts.candle.close_price == Decimal("100")


def test_previous_session_facts_are_immutable() -> None:
    facts = PreviousSessionFacts(trading_date=date(2026, 8, 5), candle=_candle(0))
    with pytest.raises(ValidationError):
        facts.trading_date = date(2026, 8, 4)  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# HistoricalContext (§31)
# --------------------------------------------------------------------------- #
def test_context_is_immutable() -> None:
    context = HistoricalContext(instrument=_instrument())
    with pytest.raises(ValidationError):
        context.series = ()  # type: ignore[misc]


def test_context_duplicate_timeframe_is_rejected() -> None:
    series_a = HistoricalSeries(timeframe=_FIVE, candles=(_candle(0),))
    series_b = HistoricalSeries(timeframe=_FIVE, candles=(_candle(5),))
    with pytest.raises(ValidationError, match="at most one series per timeframe"):
        HistoricalContext(instrument=_instrument(), series=(series_a, series_b))


def test_context_cross_instrument_series_is_rejected() -> None:
    other = HistoricalSeries(timeframe=_FIVE, candles=(_candle(0, symbol="TCS"),))
    with pytest.raises(ValidationError, match="belong to the context instrument"):
        HistoricalContext(instrument=_instrument("RELIANCE"), series=(other,))


def test_context_previous_session_instrument_mismatch_is_rejected() -> None:
    facts = PreviousSessionFacts(trading_date=date(2026, 8, 5), candle=_candle(0, symbol="TCS"))
    with pytest.raises(ValidationError, match="previous-session facts must belong"):
        HistoricalContext(instrument=_instrument("RELIANCE"), previous_session=facts)


def test_context_series_ordering_is_deterministic() -> None:
    five = HistoricalSeries(timeframe=_FIVE, candles=(_candle(0),))
    fifteen = HistoricalSeries(timeframe=_FIFTEEN, candles=(_candle(0, width=15),))
    forward = HistoricalContext(instrument=_instrument(), series=(five, fifteen))
    reverse = HistoricalContext(instrument=_instrument(), series=(fifteen, five))
    assert [s.timeframe for s in forward.series] == [_FIVE, _FIFTEEN]
    assert forward.series == reverse.series


def test_context_explicitly_empty_is_valid() -> None:
    context = HistoricalContext(instrument=_instrument())
    assert context.previous_session is None
    assert context.series == ()
