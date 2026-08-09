"""Tests for canonical market-semantic validation (docs/06 §9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.market_engine.state import InstrumentState
from app.market_engine.validation import ValidationOutcome, classify
from app.schemas.market_data import Instrument, Quote, Tick

_NOW = datetime(2026, 8, 6, 7, 0, tzinfo=UTC)
_EVENT_TIME = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _tick(*, event_timestamp: datetime = _EVENT_TIME, price: str = "100.5") -> Tick:
    return Tick(
        instrument=_instrument(),
        event_timestamp=event_timestamp,
        last_price=Decimal(price),
        traded_quantity=10,
    )


def test_unknown_instrument_is_invalid() -> None:
    outcome = classify(_tick(), known=False, state=None, now=_NOW)
    assert outcome is ValidationOutcome.INVALID


def test_first_event_for_known_instrument_is_accepted() -> None:
    outcome = classify(_tick(), known=True, state=None, now=_NOW)
    assert outcome is ValidationOutcome.ACCEPT


def test_non_finite_price_is_rejected_by_the_canonical_contract() -> None:
    # Finiteness/positivity are guaranteed upstream: the canonical Tick contract
    # rejects a non-finite price at construction, so the engine never sees one.
    with pytest.raises(ValidationError):
        _tick(price="Infinity")


def test_implausible_future_timestamp_is_invalid() -> None:
    future = _tick(event_timestamp=_NOW + timedelta(minutes=5))
    assert classify(future, known=True, state=None, now=_NOW) is ValidationOutcome.INVALID


def test_exact_repeat_is_duplicate() -> None:
    tick = _tick()
    state = InstrumentState(
        instrument=_instrument(), latest_tick=tick, last_event_timestamp=_EVENT_TIME
    )
    assert classify(tick, known=True, state=state, now=_NOW) is ValidationOutcome.DUPLICATE


def test_older_than_current_state_is_stale() -> None:
    state = InstrumentState(
        instrument=_instrument(),
        latest_tick=_tick(),
        last_event_timestamp=_EVENT_TIME,
    )
    older = _tick(event_timestamp=_EVENT_TIME - timedelta(seconds=1), price="101")
    assert classify(older, known=True, state=state, now=_NOW) is ValidationOutcome.STALE


def test_same_timestamp_different_payload_is_accepted() -> None:
    state = InstrumentState(
        instrument=_instrument(),
        latest_tick=_tick(),
        last_event_timestamp=_EVENT_TIME,
    )
    quote = Quote(
        instrument=_instrument(),
        event_timestamp=_EVENT_TIME,
        bid_price=Decimal("100"),
        ask_price=Decimal("101"),
        bid_quantity=1,
        ask_quantity=1,
    )
    assert classify(quote, known=True, state=state, now=_NOW) is ValidationOutcome.ACCEPT


def test_newer_event_is_accepted() -> None:
    state = InstrumentState(
        instrument=_instrument(),
        latest_tick=_tick(),
        last_event_timestamp=_EVENT_TIME,
    )
    newer = _tick(event_timestamp=_EVENT_TIME + timedelta(seconds=1), price="102")
    assert classify(newer, known=True, state=state, now=_NOW) is ValidationOutcome.ACCEPT
