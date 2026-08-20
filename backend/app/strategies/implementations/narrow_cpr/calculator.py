"""Pure Central Pivot Range (CPR) calculator (ADR-007 Narrow CPR spec NCR2-NCR5).

A dependency-free, deterministic value calculator over ``Decimal`` OHLC inputs. It has
no datetime, no provider type, no ``MarketContext`` dependency, and no configuration
dependency — the mathematics are a fixed domain contract, not tunable settings (NCR18).

CPR (from a previous completed session's high/low/close)::

    P  = (H + L + C) / 3            # pivot
    BC = (H + L) / 2
    TC = 2·P − BC

``TC`` and ``BC`` may invert by session geometry, so the range is normalised
orientation-independently (NCR3)::

    cpr_bottom = min(BC, TC)
    cpr_top    = max(BC, TC)
    cpr_width  = cpr_top − cpr_bottom            # = |TC − BC|, always ≥ 0

The primary narrowness metric is the pivot-normalised width percentage (NCR5), which is
cross-stock comparable because it is dimensionless (a fraction of price)::

    cpr_width_pct = cpr_width / P · 100          # smaller ⇒ narrower

All arithmetic is ``Decimal`` (NCR4): a fixed working precision is applied via a local
context so the result never depends on an ambient/mutated global context, and no
value is quantised — rounding, if any, is a caller/display concern. A zero-width CPR
(``BC == TC``) is valid and maximally narrow (NCR21), not an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

# Fixed working precision for non-terminating divisions; deterministic and independent
# of any ambient decimal context (NCR19/NCR22 determinism).
_PRECISION = 28
_TWO = Decimal(2)
_THREE = Decimal(3)
_HUNDRED = Decimal(100)


class NarrowCprInputError(ValueError):
    """Raised when CPR inputs are not a valid, well-formed OHLC triple.

    The calculator is a standalone pure component and validates its own domain (NCR4):
    prices must be strictly positive and ``low ≤ close ≤ high``. It never repairs or
    min/max malformed data into validity. In the strategy path this is unreachable —
    a canonical :class:`~app.schemas.market_data.Candle` already proves these invariants.
    """


@dataclass(frozen=True, slots=True)
class CprResult:
    """The immutable CPR geometry derived from one session's high/low/close.

    Attributes:
        pivot: ``(H + L + C) / 3``.
        bc: Bottom-central raw value ``(H + L) / 2``.
        tc: Top-central raw value ``2·P − BC`` (may be below ``bc``).
        cpr_bottom: ``min(bc, tc)``.
        cpr_top: ``max(bc, tc)``.
        cpr_width: ``cpr_top − cpr_bottom`` (``≥ 0``; zero is valid, maximally narrow).
        cpr_width_pct: Pivot-normalised width percentage ``cpr_width / pivot · 100``.
    """

    pivot: Decimal
    bc: Decimal
    tc: Decimal
    cpr_bottom: Decimal
    cpr_top: Decimal
    cpr_width: Decimal
    cpr_width_pct: Decimal


def _validate(high: Decimal, low: Decimal, close: Decimal) -> None:
    """Reject a malformed OHLC triple, failing closed (never repairs)."""
    if high <= 0 or low <= 0 or close <= 0:
        raise NarrowCprInputError("CPR inputs must be strictly positive prices")
    if high < low:
        raise NarrowCprInputError("CPR high must be greater than or equal to low")
    if not low <= close <= high:
        raise NarrowCprInputError("CPR close must lie within the high-low range")


def compute_cpr(high: Decimal, low: Decimal, close: Decimal) -> CprResult:
    """Compute the CPR geometry for a session's ``high``/``low``/``close`` (pure).

    Args:
        high: The session high (strictly positive).
        low: The session low (strictly positive, ``≤ high``).
        close: The session close (within ``[low, high]``).

    Returns:
        The immutable :class:`CprResult` (unrounded Decimal values).

    Raises:
        NarrowCprInputError: If the OHLC triple is not well-formed.
    """
    _validate(high, low, close)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        pivot = (high + low + close) / _THREE
        bc = (high + low) / _TWO
        tc = _TWO * pivot - bc
        cpr_bottom = min(bc, tc)
        cpr_top = max(bc, tc)
        cpr_width = cpr_top - cpr_bottom
        cpr_width_pct = cpr_width / pivot * _HUNDRED
    return CprResult(
        pivot=pivot,
        bc=bc,
        tc=tc,
        cpr_bottom=cpr_bottom,
        cpr_top=cpr_top,
        cpr_width=cpr_width,
        cpr_width_pct=cpr_width_pct,
    )
