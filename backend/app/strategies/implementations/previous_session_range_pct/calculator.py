"""Pure previous-session range calculator (ADR-007 Previous Session Range % spec PSR2-PSR4).

A dependency-free, deterministic value calculator over ``Decimal`` OHL inputs from one
completed session. No datetime, no provider type, no ``MarketContext``, no configuration —
the mathematics are a fixed domain contract, not tunable settings (PSR9).

From a previous completed session's open/high/low::

    previous_range     = high - low
    previous_range_pct = previous_range / open * 100      # range as a % of the session open

The percentage is price-scale invariant (a fraction of price), so it is cross-stock
comparable (PSR3). All arithmetic is ``Decimal`` (PSR4): a fixed working precision is
applied via a local context so the result never depends on an ambient/mutated global
context, and no value is quantised — rounding, if any, is a caller/display concern. A
zero-range session (``high == low``) is valid and yields ``0`` (PSR / §7), not an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

# Fixed working precision for non-terminating divisions; deterministic and independent
# of any ambient decimal context (PSR4 determinism).
_PRECISION = 28
_HUNDRED = Decimal(100)


class PreviousSessionRangeInputError(ValueError):
    """Raised when the range inputs are not a valid, well-formed OHL triple.

    The calculator is a standalone pure component and validates its own domain (PSR4):
    ``open`` must be strictly positive and ``high >= low``. It never repairs or min/max
    malformed data into validity. In the strategy path this is unreachable — a canonical
    :class:`~app.schemas.market_data.Candle` already proves these invariants.
    """


@dataclass(frozen=True, slots=True)
class PreviousSessionRangeResult:
    """The immutable range geometry derived from one session's open/high/low.

    Attributes:
        previous_range: ``high - low`` (``>= 0``; zero is valid).
        previous_range_pct: Open-normalised range percentage ``(high - low) / open * 100``.
    """

    previous_range: Decimal
    previous_range_pct: Decimal


def _validate(open_price: Decimal, high: Decimal, low: Decimal) -> None:
    """Reject a malformed OHL triple, failing closed (never repairs)."""
    if open_price <= 0:
        raise PreviousSessionRangeInputError("previous-session open must be strictly positive")
    if high < low:
        raise PreviousSessionRangeInputError(
            "previous-session high must be greater than or equal to low"
        )


def compute_previous_session_range(
    open_price: Decimal, high: Decimal, low: Decimal
) -> PreviousSessionRangeResult:
    """Compute the open-normalised range geometry for a session (pure).

    Args:
        open_price: The session open (strictly positive).
        high: The session high.
        low: The session low (``<= high``).

    Returns:
        The immutable :class:`PreviousSessionRangeResult` (unrounded Decimal values).

    Raises:
        PreviousSessionRangeInputError: If the OHL triple is not well-formed.
    """
    _validate(open_price, high, low)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        previous_range = high - low
        previous_range_pct = previous_range / open_price * _HUNDRED
    return PreviousSessionRangeResult(
        previous_range=previous_range,
        previous_range_pct=previous_range_pct,
    )
