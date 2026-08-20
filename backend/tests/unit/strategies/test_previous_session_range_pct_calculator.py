"""Previous Session Range % pure-calculator tests (ADR-007 PSR spec PSR2-PSR4)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.strategies.implementations.previous_session_range_pct import (
    PreviousSessionRangeInputError,
    compute_previous_session_range,
)


# A — standard range percentage.
def test_standard_range_percentage() -> None:
    result = compute_previous_session_range(Decimal("100"), Decimal("120"), Decimal("110"))
    assert result.previous_range == Decimal("10")
    assert result.previous_range_pct == Decimal("10")


# B — zero-range authoritative session is valid (not malformed).
def test_zero_range_is_valid() -> None:
    result = compute_previous_session_range(Decimal("100"), Decimal("100"), Decimal("100"))
    assert result.previous_range == Decimal("0")
    assert result.previous_range_pct == Decimal("0")


# C — Decimal-only arithmetic (exact, no float).
def test_decimal_only_arithmetic() -> None:
    result = compute_previous_session_range(Decimal("100"), Decimal("120"), Decimal("110"))
    assert isinstance(result.previous_range, Decimal)
    assert isinstance(result.previous_range_pct, Decimal)


# C — non-terminating division stays exact-Decimal and deterministic.
def test_non_terminating_division_is_deterministic() -> None:
    first = compute_previous_session_range(Decimal("3"), Decimal("4"), Decimal("3"))
    second = compute_previous_session_range(Decimal("3"), Decimal("4"), Decimal("3"))
    assert first.previous_range_pct == second.previous_range_pct
    assert str(first.previous_range_pct).startswith("33.33")


# D — cross-price scaling invariance (dimensionless ratio).
def test_price_scale_invariance() -> None:
    small = compute_previous_session_range(Decimal("100"), Decimal("120"), Decimal("110"))
    large = compute_previous_session_range(Decimal("1000"), Decimal("1200"), Decimal("1100"))
    assert small.previous_range_pct == large.previous_range_pct == Decimal("10")


# E — malformed input fails closed (never repaired to zero).
def test_non_positive_open_fails_closed() -> None:
    with pytest.raises(PreviousSessionRangeInputError):
        compute_previous_session_range(Decimal("0"), Decimal("120"), Decimal("110"))


def test_high_below_low_fails_closed() -> None:
    with pytest.raises(PreviousSessionRangeInputError):
        compute_previous_session_range(Decimal("100"), Decimal("90"), Decimal("110"))
