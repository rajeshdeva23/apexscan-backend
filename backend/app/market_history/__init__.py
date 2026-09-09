"""Broker-neutral historical OHLCV foundation (DECOUPLING PHASE F).

Durable multi-day daily-bar history for the instruments of an effective Phase-E UniverseSnapshot:
canonical bars/series, trading-calendar-aware coverage, a durable store contract (+ file-backed
reference impl), a provider-neutral source boundary, and a coverage/backfill service. It answers
"does instrument X have the required N trading days?" — never strategy readiness (Phase G). Off by
default: nothing here is constructed by production composition, contacts a provider, or activates
IPC. Universe membership != historical completeness != strategy readiness.
"""

from __future__ import annotations

from app.market_history.coverage import (
    HistoricalCoverage,
    compute_coverage,
    previous_trading_date,
    required_trading_dates,
)
from app.market_history.diagnostics import HistoryDiagnostics, HistoryMetrics
from app.market_history.models import (
    HistoricalDailyBar,
    HistoricalSeries,
    HistoryRequirement,
    PriceAdjustment,
)
from app.market_history.service import (
    AdjustedDataUnavailableError,
    BackfillReport,
    HistoricalService,
    InstrumentBackfillResult,
    PreviousCloseComparison,
)
from app.market_history.source import (
    HistoricalDataSource,
    HistoricalSourceError,
    InMemoryHistoricalDataSource,
)
from app.market_history.store import (
    FileHistoricalBarStore,
    HistoricalBarStore,
    UpsertOutcome,
)

__all__ = [
    "AdjustedDataUnavailableError",
    "BackfillReport",
    "FileHistoricalBarStore",
    "HistoricalBarStore",
    "HistoricalCoverage",
    "HistoricalDailyBar",
    "HistoricalDataSource",
    "HistoricalSeries",
    "HistoricalService",
    "HistoricalSourceError",
    "HistoryDiagnostics",
    "HistoryMetrics",
    "HistoryRequirement",
    "InMemoryHistoricalDataSource",
    "InstrumentBackfillResult",
    "PreviousCloseComparison",
    "PriceAdjustment",
    "UpsertOutcome",
    "compute_coverage",
    "previous_trading_date",
    "required_trading_dates",
]
