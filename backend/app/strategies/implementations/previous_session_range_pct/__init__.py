"""Previous Session Range % strategy implementation (V1; ADR-007 PSR strategy specification).

Public surface: the pure ``compute_previous_session_range`` calculator + its
``PreviousSessionRangeResult``, the ``PreviousSessionRangePctConfiguration``, and the
``PreviousSessionRangePctStrategy`` plug-in. Isolated subpackage — it imports only its own
subtree, the read-only shared strategy contracts, canonical schemas, and the read-only
Market-Engine value/fact modules.
"""

from __future__ import annotations

from app.strategies.implementations.previous_session_range_pct.calculator import (
    PreviousSessionRangeInputError,
    PreviousSessionRangeResult,
    compute_previous_session_range,
)
from app.strategies.implementations.previous_session_range_pct.configuration import (
    PreviousSessionRangePctConfiguration,
)
from app.strategies.implementations.previous_session_range_pct.strategy import (
    PreviousSessionRangePctStrategy,
)

__all__ = [
    "PreviousSessionRangeInputError",
    "PreviousSessionRangePctConfiguration",
    "PreviousSessionRangePctStrategy",
    "PreviousSessionRangeResult",
    "compute_previous_session_range",
]
