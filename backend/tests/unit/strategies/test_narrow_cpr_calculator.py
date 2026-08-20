"""Pure CPR calculator tests (ADR-007 Narrow CPR spec NCR2-NCR5, NCR21)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.strategies.implementations.narrow_cpr.calculator import (
    NarrowCprInputError,
    compute_cpr,
)


def _d(value: str) -> Decimal:
    return Decimal(value)


# A / B — canonical formula, normal orientation (TC > BC).
# H=120 L=68 C=112 -> P=100, BC=94, TC=106, width=12, width_pct=12.
def test_canonical_formula_normal_orientation() -> None:
    result = compute_cpr(_d("120"), _d("68"), _d("112"))
    assert result.pivot == _d("100")
    assert result.bc == _d("94")
    assert result.tc == _d("106")
    assert result.cpr_bottom == _d("94")
    assert result.cpr_top == _d("106")
    assert result.cpr_width == _d("12")
    assert result.cpr_width_pct == _d("12")
    # Normal orientation: top is TC, bottom is BC.
    assert result.cpr_top == result.tc
    assert result.cpr_bottom == result.bc


# C — TC < BC normalization. H=140 L=72 C=88 -> BC=106, TC=94.
def test_tc_below_bc_is_normalized() -> None:
    result = compute_cpr(_d("140"), _d("72"), _d("88"))
    assert result.bc == _d("106")
    assert result.tc == _d("94")
    # Normalized: bottom is the (smaller) TC, top is the (larger) BC.
    assert result.cpr_bottom == result.tc == _d("94")
    assert result.cpr_top == result.bc == _d("106")
    assert result.cpr_width == _d("12")
    assert result.cpr_width >= 0


# D — zero-width CPR is valid (BC == TC). H=110 L=90 C=100 -> P=BC=TC=100.
def test_zero_width_cpr_is_valid() -> None:
    result = compute_cpr(_d("110"), _d("90"), _d("100"))
    assert result.bc == result.tc == _d("100")
    assert result.cpr_width == _d("0")
    assert result.cpr_width_pct == _d("0")


# E — Decimal throughout, no float.
def test_values_are_decimal_never_float() -> None:
    result = compute_cpr(_d("100.25"), _d("80.10"), _d("95.55"))
    for value in (
        result.pivot,
        result.bc,
        result.tc,
        result.cpr_bottom,
        result.cpr_top,
        result.cpr_width,
        result.cpr_width_pct,
    ):
        assert isinstance(value, Decimal)


# F — cross-price-scale: geometry scaled x10 yields identical width_pct, different raw width.
def test_cross_price_scale_equal_width_pct() -> None:
    base = compute_cpr(_d("120"), _d("68"), _d("112"))
    scaled = compute_cpr(_d("1200"), _d("680"), _d("1120"))
    assert scaled.cpr_width == _d("120")  # raw width scales with price
    assert base.cpr_width == _d("12")
    assert base.cpr_width_pct == scaled.cpr_width_pct == _d("12")  # normalized is invariant


@pytest.mark.parametrize(
    ("high", "low", "close"),
    [
        pytest.param("0", "0", "0", id="non-positive"),
        pytest.param("-1", "-2", "-1", id="negative"),
        pytest.param("100", "120", "110", id="high-below-low"),
        pytest.param("120", "100", "130", id="close-above-high"),
        pytest.param("120", "100", "90", id="close-below-low"),
    ],
)
def test_malformed_input_fails_closed(high: str, low: str, close: str) -> None:
    # The pure calculator validates its own domain and never repairs bad data.
    with pytest.raises(NarrowCprInputError):
        compute_cpr(_d(high), _d(low), _d(close))
