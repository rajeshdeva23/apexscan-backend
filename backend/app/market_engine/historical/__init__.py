"""Historical Context foundation for the Market Engine (P4.5A).

Pure, deterministic, provider-free building blocks: the requirement model and
its deterministic union, the immutable historical-context value objects, and
previous-trading-day resolution over an authoritative calendar window. This
slice performs no historical provider I/O, no caching, no resampling, and no
reconciliation — those land in later P4.5 slices (ADR-006 §7).
"""

from app.market_engine.historical.calendar_window import (
    CalendarCoverage,
    HistoricalCalendarWindow,
    OutsideCalendarCoverageError,
)
from app.market_engine.historical.context import (
    HistoricalContext,
    HistoricalSeries,
    PreviousSessionFacts,
)
from app.market_engine.historical.requirements import (
    HistoricalRequirement,
    HistoricalRequirementRegistry,
    timeframe_ordering_key,
)

__all__ = [
    "CalendarCoverage",
    "HistoricalCalendarWindow",
    "HistoricalContext",
    "HistoricalRequirement",
    "HistoricalRequirementRegistry",
    "HistoricalSeries",
    "OutsideCalendarCoverageError",
    "PreviousSessionFacts",
    "timeframe_ordering_key",
]
