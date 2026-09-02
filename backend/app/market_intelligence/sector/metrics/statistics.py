"""Pure, deterministic order statistics over Decimal sequences (SECTOR-3).

No library defaults decide business semantics: median, MAD, and IQR are implemented
explicitly and are hand-verifiable. The quartile convention is **Tukey exclusive
hinges** — split the sorted sample at the median, dropping the middle element when the
count is odd, and take the median of each half. IQR is undefined for fewer than two
values (returned as ``None``, never fabricated).

All arithmetic is :class:`decimal.Decimal` under the caller's active context, so the
same inputs yield the same outputs across environments.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

_TWO = Decimal(2)


def median(values: Sequence[Decimal]) -> Decimal | None:
    """Return the median (odd: middle; even: mean of the two middle), or None if empty."""
    count = len(values)
    if count == 0:
        return None
    ordered = sorted(values)
    mid = count // 2
    if count % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / _TWO


def mad(values: Sequence[Decimal]) -> Decimal | None:
    """Return the median absolute deviation about the median, or None if empty.

    ``median(|x_i - median(x)|)``. For a single value this is ``0`` (no deviation).
    """
    center = median(values)
    if center is None:
        return None
    return median([abs(value - center) for value in values])


def _halves(values: Sequence[Decimal]) -> tuple[list[Decimal], list[Decimal]]:
    ordered = sorted(values)
    count = len(ordered)
    half = count // 2
    lower = ordered[:half]
    upper = ordered[count - half :]  # drops the middle element when count is odd
    return lower, upper


def iqr(values: Sequence[Decimal]) -> Decimal | None:
    """Return the interquartile range (Tukey exclusive hinges), or None if N < 2.

    Q1 = median of the lower half, Q3 = median of the upper half, where the halves are
    the sorted values below/above the overall median (the middle value is excluded when
    the count is odd). Undefined for fewer than two values.
    """
    if len(values) < 2:
        return None
    lower, upper = _halves(values)
    q1, q3 = median(lower), median(upper)
    if q1 is None or q3 is None:
        return None
    return q3 - q1
