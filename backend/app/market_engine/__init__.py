"""Market Engine — the deterministic foundation (P4.1).

This package currently provides only the deterministic building blocks the
engine is built on: the immutable, versioned :class:`MarketContext`, an
injectable UTC :class:`Clock`, a replay-safe :class:`SequenceGenerator`, and the
context-lifecycle events. It consumes only canonical contracts and its permitted
backend seams (docs/03 §3.6) and computes no market logic — no tick routing,
candle aggregation, session state, historical warmup, features, or strategy
evaluation. Those land additively in later slices.
"""

from app.market_engine.candle_engine import CandleEngine
from app.market_engine.clock import Clock, ManualClock, SystemClock
from app.market_engine.context import (
    CandleQuality,
    IncompleteCandle,
    MarketContext,
    MarketFact,
    MarketState,
    PartialCandle,
    SessionContext,
    TimeframeCandles,
)
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.historical import (
    CalendarCoverage,
    HistoricalCalendarWindow,
    HistoricalContext,
    HistoricalRequirement,
    HistoricalRequirementRegistry,
    HistoricalSeries,
    OutsideCalendarCoverageError,
    PreviousSessionFacts,
)
from app.market_engine.sequence import MonotonicSequence, SequenceGenerator
from app.market_engine.session import (
    MarketSessionClassifier,
    SessionSchedule,
    TradingCalendar,
)
from app.market_engine.state import InstrumentState, InstrumentStateRegistry
from app.market_engine.tick_engine import ProcessResult, TickEngine
from app.market_engine.timeframe import Timeframe, TimeframeKind
from app.market_engine.validation import ValidationOutcome

__all__ = [
    "CalendarCoverage",
    "CandleEngine",
    "CandleQuality",
    "Clock",
    "HistoricalCalendarWindow",
    "HistoricalContext",
    "HistoricalRequirement",
    "HistoricalRequirementRegistry",
    "HistoricalSeries",
    "IncompleteCandle",
    "InstrumentState",
    "InstrumentStateRegistry",
    "ManualClock",
    "MarketContext",
    "MarketContextCreated",
    "MarketContextUpdated",
    "MarketFact",
    "MarketSessionClassifier",
    "MarketState",
    "MonotonicSequence",
    "OutsideCalendarCoverageError",
    "PartialCandle",
    "PreviousSessionFacts",
    "ProcessResult",
    "SequenceGenerator",
    "SessionContext",
    "SessionSchedule",
    "SystemClock",
    "TickEngine",
    "Timeframe",
    "TimeframeCandles",
    "TimeframeKind",
    "TradingCalendar",
    "ValidationOutcome",
]
