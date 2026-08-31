"""Protocol-aware price canonicalization for the evidence comparison (DEPLOY-10 R4C).

The Dhan live feed carries session OHLC as IEEE-754 **32-bit** floats (``<f`` in the
packet structs); the adapter decodes them via ``Decimal(str(float))``, which widens the
float32 to binary64 and prints its full expansion — e.g. the wire price 212.37 becomes
``Decimal("212.3699951171875")``. The REST oracle returns a clean ``Decimal("212.37")``.
Exact Decimal equality therefore reports a false mismatch for identical prices.

Canonicalization compares the **float32 wire representation** of both values: two prices
are protocol-equivalent iff they pack to the same 4 IEEE-754 float32 bytes. This is
deterministic and needs no arbitrary decimal rounding and no tick-size assumption — a
genuinely different price rounds to a different float32 and remains a mismatch. Non-finite
values (NaN/±Inf) are never valid market prices and are rejected (no canonical form).
"""

from __future__ import annotations

import math
import struct
from decimal import Decimal

_FLOAT32 = struct.Struct("<f")


def is_finite_price(value: Decimal | float) -> bool:
    """Return whether ``value`` is a finite number usable as a market price."""
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def float32_hex(value: Decimal | float | None) -> str | None:
    """Return the 8-char hex of ``value``'s IEEE-754 float32 encoding, or ``None``.

    ``None`` (fail closed) for a missing, non-finite, or out-of-float32-range value: a value
    can be finite as binary64 yet overflow float32 (e.g. ``1e100``), for which ``struct.pack``
    raises. Such a value has no canonical wire form — it can never be a real Dhan feed price —
    so it is never treated as equivalent to anything and its comparison stays a MISMATCH.
    """
    if value is None or not is_finite_price(value):
        return None
    try:
        return _FLOAT32.pack(float(value)).hex()
    except (OverflowError, ValueError, struct.error):
        return None


def float32_equivalent(left: Decimal | float | None, right: Decimal | float | None) -> bool:
    """Return whether both values encode to the identical float32 wire representation."""
    left_bits = float32_hex(left)
    right_bits = float32_hex(right)
    return left_bits is not None and left_bits == right_bits
