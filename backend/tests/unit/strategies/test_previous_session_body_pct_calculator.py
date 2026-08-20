"""Previous Session Body % pure-calculator tests (ADR-007 PSB spec PSB2-PSB7)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.strategies.implementations.previous_session_body_pct import (
    PreviousSessionBodyInputError,
    compute_previous_session_body,
)


# A/B — direction-neutral: equal-magnitude up and down sessions give the same body %.
def test_direction_neutral_equal_magnitude() -> None:
    up = compute_previous_session_body(Decimal("100"), Decimal("110"))
    down = compute_previous_session_body(Decimal("100"), Decimal("90"))
    assert up.previous_body == down.previous_body == Decimal("10")
    assert up.previous_body_pct == down.previous_body_pct == Decimal("10")


# C — zero body (doji) is valid.
def test_zero_body_is_valid() -> None:
    result = compute_previous_session_body(Decimal("100"), Decimal("100"))
    assert result.previous_body == Decimal("0")
    assert result.previous_body_pct == Decimal("0")


# D/H — fractional inputs, Decimal result types.
def test_fractional_decimal_types() -> None:
    result = compute_previous_session_body(Decimal("100.50"), Decimal("101.00"))
    assert isinstance(result.previous_body, Decimal)
    assert isinstance(result.previous_body_pct, Decimal)
    assert result.previous_body == Decimal("0.50")


# E — non-terminating division is deterministic.
def test_non_terminating_division_is_deterministic() -> None:
    first = compute_previous_session_body(Decimal("3"), Decimal("4"))
    second = compute_previous_session_body(Decimal("3"), Decimal("4"))
    assert first.previous_body_pct == second.previous_body_pct
    assert str(first.previous_body_pct).startswith("33.33")


# F — cross-price scaling invariance.
def test_price_scale_invariance() -> None:
    small = compute_previous_session_body(Decimal("100"), Decimal("110"))
    large = compute_previous_session_body(Decimal("1000"), Decimal("1100"))
    assert small.previous_body == Decimal("10")
    assert large.previous_body == Decimal("100")
    assert small.previous_body_pct == large.previous_body_pct == Decimal("10")


# G — non-positive open fails closed.
def test_non_positive_open_fails_closed() -> None:
    with pytest.raises(PreviousSessionBodyInputError):
        compute_previous_session_body(Decimal("0"), Decimal("110"))
