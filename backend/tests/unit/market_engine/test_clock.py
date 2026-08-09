"""Tests for the injectable, UTC-internal clock abstraction (docs/06 §12.7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.market_engine.clock import ManualClock, SystemClock


def test_system_clock_returns_timezone_aware_utc() -> None:
    moment = SystemClock().now()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == timedelta(0)


def test_manual_clock_is_deterministic() -> None:
    clock = ManualClock(datetime(2026, 8, 6, 6, 30, tzinfo=UTC))
    assert clock.now() == datetime(2026, 8, 6, 6, 30, tzinfo=UTC)
    assert clock.now() == clock.now()


def test_manual_clock_set_and_advance_are_explicit() -> None:
    clock = ManualClock(datetime(2026, 8, 6, 6, 30, tzinfo=UTC))
    clock.advance(timedelta(minutes=5))
    assert clock.now() == datetime(2026, 8, 6, 6, 35, tzinfo=UTC)
    clock.set(datetime(2026, 8, 6, 10, 0, tzinfo=UTC))
    assert clock.now() == datetime(2026, 8, 6, 10, 0, tzinfo=UTC)


def test_manual_clock_normalises_non_utc_input_to_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    clock = ManualClock(datetime(2026, 8, 6, 12, 0, tzinfo=ist))
    assert clock.now() == datetime(2026, 8, 6, 6, 30, tzinfo=UTC)


def test_manual_clock_rejects_naive_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ManualClock(datetime(2026, 8, 6, 6, 30))  # noqa: DTZ001 (intentionally naive)
