"""Pure previous-session body calculator (ADR-007 Previous Session Body % spec PSB2-PSB6).

A dependency-free, deterministic value calculator over ``Decimal`` open/close inputs from one
completed session. No datetime, no provider type, no ``MarketContext``, no configuration —
the mathematics are a fixed domain contract, not tunable settings (PSB9).

From a previous completed session's open/close::

    previous_body     = abs(close - open)
    previous_body_pct = previous_body / open * 100      # body as a % of the session open

The absolute value makes the metric **direction-neutral** (PSB4): an equal-magnitude up or
down session yields the same body %. It is price-scale invariant (a fraction of price), so it
is cross-stock comparable (PSB7). All arithmetic is ``Decimal`` (PSB3): a fixed working
precision is applied via a local context so the result never depends on an ambient/mutated
global context, and no value is quantised. A zero-body session (``open == close``) is valid and
yields ``0`` (PSB5), not an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

# Fixed working precision for non-terminating divisions; deterministic and independent
# of any ambient decimal context (PSB3 determinism).
_PRECISION = 28
_HUNDRED = Decimal(100)


class PreviousSessionBodyInputError(ValueError):
    """Raised when the body inputs are not a valid, well-formed open/close pair.

    The calculator is a standalone pure component and validates its own domain (PSB6):
    ``open`` must be strictly positive. It never repairs malformed data into validity. In
    the strategy path this is unreachable — a canonical
    :class:`~app.schemas.market_data.Candle` already proves ``open > 0``.
    """


@dataclass(frozen=True, slots=True)
class PreviousSessionBodyResult:
    """The immutable body geometry derived from one session's open/close.

    Attributes:
        previous_body: ``abs(close - open)`` (``>= 0``; zero is valid).
        previous_body_pct: Open-normalised absolute body percentage ``|close - open| / open * 100``.
    """

    previous_body: Decimal
    previous_body_pct: Decimal


def _validate(open_price: Decimal) -> None:
    """Reject a non-positive open, failing closed (never repairs)."""
    if open_price <= 0:
        raise PreviousSessionBodyInputError("previous-session open must be strictly positive")


def compute_previous_session_body(
    open_price: Decimal, close_price: Decimal
) -> PreviousSessionBodyResult:
    """Compute the open-normalised absolute body geometry for a session (pure; direction-neutral).

    Args:
        open_price: The session open (strictly positive).
        close_price: The session close.

    Returns:
        The immutable :class:`PreviousSessionBodyResult` (unrounded Decimal values).

    Raises:
        PreviousSessionBodyInputError: If ``open_price`` is not strictly positive.
    """
    _validate(open_price)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        previous_body = abs(close_price - open_price)
        previous_body_pct = previous_body / open_price * _HUNDRED
    return PreviousSessionBodyResult(
        previous_body=previous_body,
        previous_body_pct=previous_body_pct,
    )
