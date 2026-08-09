"""Canonical contract tests for Tick.session_cumulative_volume (ADR-005)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.market_data import Instrument, Tick

_EVENT_TIME = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)


def _instrument() -> Instrument:
    return Instrument(exchange="NSE", symbol="RELIANCE")


def test_tick_without_cumulative_volume_is_valid_and_defaults_to_none() -> None:
    tick = Tick(instrument=_instrument(), event_timestamp=_EVENT_TIME, last_price=Decimal("100"))
    assert tick.session_cumulative_volume is None


def test_tick_with_cumulative_volume_is_valid() -> None:
    tick = Tick(
        instrument=_instrument(),
        event_timestamp=_EVENT_TIME,
        last_price=Decimal("100"),
        traded_quantity=5,
        session_cumulative_volume=125_000,
    )
    assert tick.session_cumulative_volume == 125_000
    # LTQ semantics are unchanged and independent of cumulative volume.
    assert tick.traded_quantity == 5


def test_negative_cumulative_volume_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Tick(
            instrument=_instrument(),
            event_timestamp=_EVENT_TIME,
            last_price=Decimal("100"),
            session_cumulative_volume=-1,
        )


def test_cumulative_volume_is_immutable() -> None:
    tick = Tick(
        instrument=_instrument(),
        event_timestamp=_EVENT_TIME,
        last_price=Decimal("100"),
        session_cumulative_volume=10,
    )
    with pytest.raises(ValidationError):
        tick.session_cumulative_volume = 20  # type: ignore[misc]
