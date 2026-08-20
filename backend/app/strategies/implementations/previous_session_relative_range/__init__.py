"""Previous Session Relative Range strategy implementation (V1; ADR-007 PSRR spec).

Public surface: the pure calculator (``compute_previous_session_relative_range`` + helpers +
``PreviousSessionRelativeRangeResult`` + errors), the configuration, and the strategy plug-in.
Isolated subpackage — imports only its own subtree, the read-only shared strategy contracts,
canonical schemas, and the read-only Market-Engine value/fact modules.
"""

from __future__ import annotations

from app.strategies.implementations.previous_session_relative_range.calculator import (
    DegenerateBaselineError,
    PreviousSessionRelativeRangeInputError,
    PreviousSessionRelativeRangeResult,
    compute_previous_session_relative_range,
    median,
    range_percent,
)
from app.strategies.implementations.previous_session_relative_range.configuration import (
    PreviousSessionRelativeRangeConfiguration,
)
from app.strategies.implementations.previous_session_relative_range.strategy import (
    BASELINE_SESSIONS,
    REQUIRED_SESSIONS,
    PreviousSessionRelativeRangeStrategy,
)

__all__ = [
    "BASELINE_SESSIONS",
    "REQUIRED_SESSIONS",
    "DegenerateBaselineError",
    "PreviousSessionRelativeRangeConfiguration",
    "PreviousSessionRelativeRangeInputError",
    "PreviousSessionRelativeRangeResult",
    "PreviousSessionRelativeRangeStrategy",
    "compute_previous_session_relative_range",
    "median",
    "range_percent",
]
