"""Pure previous-session relative-range calculator (ADR-007 PSRR spec PSRR3-PSRR10).

A dependency-free, deterministic calculator over ``Decimal`` OHL inputs from completed
sessions. No datetime, provider type, ``MarketContext``, or configuration dependency.

Per session: ``range_pct = (high - low) / open * 100`` (a within-one-session ratio,
invariant to a uniform per-session corporate-action scaling factor — PSRR21). The subject
is the previous completed session (D-1); the baseline is the exact ``Decimal`` median of the
prior sessions' range percentages; the ranking metric is
``relative_range_ratio = subject_range_pct / baseline_range_pct``.

All arithmetic runs under a fixed ``localcontext(prec=28)`` (PSRR4): deterministic and
independent of any ambient context; no float, no quantisation before ranking. A zero
baseline median is a degenerate condition (division undefined) surfaced as
:class:`DegenerateBaselineError`, which the strategy maps to ``SKIPPED`` (PSRR10-B).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

_PRECISION = 28
_HUNDRED = Decimal(100)
_TWO = Decimal(2)


class PreviousSessionRelativeRangeInputError(ValueError):
    """Raised when a session's OHL is not well-formed (``open <= 0``).

    The calculator validates its own domain (PSRR3/PSRR4); it never repairs malformed
    data. In the strategy path this is unreachable — a canonical
    :class:`~app.schemas.market_data.Candle` already guarantees ``open > 0``.
    """


class DegenerateBaselineError(ValueError):
    """Raised when the baseline median range percentage is zero (ratio undefined).

    Not a malformed-input condition: the strategy catches it and returns ``SKIPPED``
    (``PREVIOUS_SESSION_RELATIVE_RANGE_DEGENERATE_BASELINE``), never fabricating a ratio
    or dividing by zero (PSRR10-B).
    """


@dataclass(frozen=True, slots=True)
class PreviousSessionRelativeRangeResult:
    """The immutable relative-range geometry (all unrounded ``Decimal``)."""

    relative_range_ratio: Decimal
    previous_range_pct: Decimal
    baseline_range_pct: Decimal


def range_percent(open_price: Decimal, high: Decimal, low: Decimal) -> Decimal:
    """Return one session's open-normalised range percentage ``(high - low) / open * 100``.

    Raises:
        PreviousSessionRelativeRangeInputError: If ``open_price`` is not strictly positive.
    """
    if open_price <= 0:
        raise PreviousSessionRelativeRangeInputError("session open must be strictly positive")
    return (high - low) / open_price * _HUNDRED


def median(values: Sequence[Decimal]) -> Decimal:
    """Return the exact ``Decimal`` median (even N -> mean of the two central values).

    Deterministic and library-free (no ``statistics``/percentile/interpolation): sort a
    copy ascending; odd N -> middle element; even N -> ``(sorted[N/2 - 1] + sorted[N/2]) / 2``.
    """
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        raise PreviousSessionRelativeRangeInputError("median requires at least one value")
    mid = count // 2
    if count % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / _TWO


def compute_previous_session_relative_range(
    subject: tuple[Decimal, Decimal, Decimal],
    baseline: Sequence[tuple[Decimal, Decimal, Decimal]],
) -> PreviousSessionRelativeRangeResult:
    """Compute the relative-range ratio for a subject session against a baseline (pure).

    Args:
        subject: The D-1 ``(open, high, low)``.
        baseline: The prior sessions' ``(open, high, low)`` triples (D-2 .. D-N).

    Returns:
        The immutable :class:`PreviousSessionRelativeRangeResult`.

    Raises:
        PreviousSessionRelativeRangeInputError: If any session's ``open`` is not positive.
        DegenerateBaselineError: If the baseline median range percentage is zero.
    """
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        previous_range_pct = range_percent(*subject)
        baseline_range_pct = median([range_percent(*ohl) for ohl in baseline])
        if baseline_range_pct == 0:
            raise DegenerateBaselineError("baseline median range percentage is zero")
        relative_range_ratio = previous_range_pct / baseline_range_pct
    return PreviousSessionRelativeRangeResult(
        relative_range_ratio=relative_range_ratio,
        previous_range_pct=previous_range_pct,
        baseline_range_pct=baseline_range_pct,
    )
