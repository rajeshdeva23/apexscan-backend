"""Previous Session Relative Range pure-calculator tests (ADR-007 PSRR spec §21)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.strategies.implementations.previous_session_relative_range import (
    DegenerateBaselineError,
    PreviousSessionRelativeRangeInputError,
    compute_previous_session_relative_range,
    median,
    range_percent,
)


def _ohl(range_value: str) -> tuple[Decimal, Decimal, Decimal]:
    """open=100, low=100, high=100+range -> range_pct == range_value (since open=100)."""
    return (Decimal("100"), Decimal("100") + Decimal(range_value), Decimal("100"))


def _baseline(range_value: str, count: int = 20) -> list[tuple[Decimal, Decimal, Decimal]]:
    return [_ohl(range_value) for _ in range(count)]


# range_percent ---------------------------------------------------------------
def test_range_percent_exact() -> None:
    assert range_percent(Decimal("100"), Decimal("120"), Decimal("110")) == Decimal("10")


def test_range_percent_scale_invariance() -> None:
    small = range_percent(Decimal("100"), Decimal("110"), Decimal("90"))
    large = range_percent(Decimal("1000"), Decimal("1100"), Decimal("900"))
    assert small == large == Decimal("20")


def test_range_percent_non_terminating_deterministic() -> None:
    first = range_percent(Decimal("3"), Decimal("4"), Decimal("3"))
    second = range_percent(Decimal("3"), Decimal("4"), Decimal("3"))
    assert first == second
    assert str(first).startswith("33.33")


def test_range_percent_invalid_open_fails_closed() -> None:
    with pytest.raises(PreviousSessionRelativeRangeInputError):
        range_percent(Decimal("0"), Decimal("110"), Decimal("90"))


# median (exact, even N=20) ---------------------------------------------------
def test_median_even_uses_two_central_values() -> None:
    # 1..20 ascending; central = 10th (=10) and 11th (=11) -> 10.5
    values = [Decimal(n) for n in range(1, 21)]
    assert median(values) == Decimal("10.5")


def test_median_is_order_independent() -> None:
    ascending = [Decimal(n) for n in range(1, 21)]
    shuffled = list(reversed(ascending))
    assert median(shuffled) == median(ascending) == Decimal("10.5")


def test_median_odd_returns_middle() -> None:
    assert median([Decimal("1"), Decimal("5"), Decimal("2")]) == Decimal("2")


# compute ---------------------------------------------------------------------
def test_ratio_less_than_one() -> None:
    result = compute_previous_session_relative_range(_ohl("10"), _baseline("20"))
    assert result.previous_range_pct == Decimal("10")
    assert result.baseline_range_pct == Decimal("20")
    assert result.relative_range_ratio == Decimal("0.5")


def test_ratio_equal_one() -> None:
    result = compute_previous_session_relative_range(_ohl("20"), _baseline("20"))
    assert result.relative_range_ratio == Decimal("1")


def test_ratio_greater_than_one() -> None:
    result = compute_previous_session_relative_range(_ohl("40"), _baseline("20"))
    assert result.relative_range_ratio == Decimal("2")


def test_zero_subject_range_is_valid_ratio_zero() -> None:
    result = compute_previous_session_relative_range(_ohl("0"), _baseline("20"))
    assert result.previous_range_pct == Decimal("0")
    assert result.relative_range_ratio == Decimal("0")


def test_individual_zero_baseline_with_nonzero_median_is_valid() -> None:
    baseline = _baseline("0", count=10) + _baseline("20", count=10)  # median of 0s and 20s = 10
    result = compute_previous_session_relative_range(_ohl("10"), baseline)
    assert result.baseline_range_pct == Decimal("10")
    assert result.relative_range_ratio == Decimal("1")


def test_zero_baseline_median_is_degenerate() -> None:
    with pytest.raises(DegenerateBaselineError):
        compute_previous_session_relative_range(_ohl("10"), _baseline("0"))


def test_result_types_are_decimal() -> None:
    result = compute_previous_session_relative_range(_ohl("10"), _baseline("20"))
    assert isinstance(result.relative_range_ratio, Decimal)
    assert isinstance(result.previous_range_pct, Decimal)
    assert isinstance(result.baseline_range_pct, Decimal)


def test_deterministic_repeat() -> None:
    a = compute_previous_session_relative_range(_ohl("7"), _baseline("13"))
    b = compute_previous_session_relative_range(_ohl("7"), _baseline("13"))
    assert a == b


def test_price_scale_invariance_end_to_end() -> None:
    base = compute_previous_session_relative_range(
        (Decimal("100"), Decimal("110"), Decimal("90")),
        [(Decimal("100"), Decimal("120"), Decimal("80"))] * 20,
    )
    scaled = compute_previous_session_relative_range(
        (Decimal("1000"), Decimal("1100"), Decimal("900")),
        [(Decimal("1000"), Decimal("1200"), Decimal("800"))] * 20,
    )
    assert base.relative_range_ratio == scaled.relative_range_ratio
