"""Narrow CPR strategy implementation (V1; ADR-007 Narrow CPR strategy specification).

Public surface: the pure ``compute_cpr`` calculator + its ``CprResult``, the
``NarrowCprConfiguration``, and the ``NarrowCprStrategy`` plug-in. Isolated subpackage —
it imports only its own subtree, the read-only shared strategy contracts, canonical
schemas, and the read-only Market-Engine value/fact modules.
"""

from __future__ import annotations

from app.strategies.implementations.narrow_cpr.calculator import (
    CprResult,
    NarrowCprInputError,
    compute_cpr,
)
from app.strategies.implementations.narrow_cpr.configuration import NarrowCprConfiguration
from app.strategies.implementations.narrow_cpr.strategy import NarrowCprStrategy

__all__ = [
    "CprResult",
    "NarrowCprConfiguration",
    "NarrowCprInputError",
    "NarrowCprStrategy",
    "compute_cpr",
]
