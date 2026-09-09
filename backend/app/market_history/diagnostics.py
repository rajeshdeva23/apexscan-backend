"""Bounded, credential-free historical diagnostics (DECOUPLING PHASE F).

Aggregate totals only — never per-instrument metric labels. Detailed per-instrument results are
returned from explicit service calls (:class:`BackfillReport`), not accumulated here.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class HistoryDiagnostics(BaseModel):
    """Bounded snapshot of cumulative historical fetch/coverage counters."""

    model_config = ConfigDict(frozen=True)

    fetch_attempts: int
    fetch_successes: int
    fetch_failures: int
    bars_received: int
    bars_inserted: int
    bars_unchanged: int
    bar_conflicts: int
    coverage_checks: int
    complete_instruments: int
    incomplete_instruments: int
    missing_trading_days: int
    last_fetch_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None


class HistoryMetrics:
    """Mutable cumulative counters (fixed field set; no per-instrument growth)."""

    __slots__ = (
        "fetch_attempts",
        "fetch_successes",
        "fetch_failures",
        "bars_received",
        "bars_inserted",
        "bars_unchanged",
        "bar_conflicts",
        "coverage_checks",
        "complete_instruments",
        "incomplete_instruments",
        "missing_trading_days",
        "last_fetch_at",
        "last_success_at",
        "last_failure_at",
    )

    def __init__(self) -> None:
        self.fetch_attempts = 0
        self.fetch_successes = 0
        self.fetch_failures = 0
        self.bars_received = 0
        self.bars_inserted = 0
        self.bars_unchanged = 0
        self.bar_conflicts = 0
        self.coverage_checks = 0
        self.complete_instruments = 0
        self.incomplete_instruments = 0
        self.missing_trading_days = 0
        self.last_fetch_at: datetime | None = None
        self.last_success_at: datetime | None = None
        self.last_failure_at: datetime | None = None

    def snapshot(self) -> HistoryDiagnostics:
        """Freeze current counters into an immutable diagnostics model."""
        return HistoryDiagnostics(
            fetch_attempts=self.fetch_attempts,
            fetch_successes=self.fetch_successes,
            fetch_failures=self.fetch_failures,
            bars_received=self.bars_received,
            bars_inserted=self.bars_inserted,
            bars_unchanged=self.bars_unchanged,
            bar_conflicts=self.bar_conflicts,
            coverage_checks=self.coverage_checks,
            complete_instruments=self.complete_instruments,
            incomplete_instruments=self.incomplete_instruments,
            missing_trading_days=self.missing_trading_days,
            last_fetch_at=self.last_fetch_at,
            last_success_at=self.last_success_at,
            last_failure_at=self.last_failure_at,
        )
