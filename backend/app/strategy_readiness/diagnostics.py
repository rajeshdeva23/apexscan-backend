"""Bounded readiness diagnostics (DECOUPLING PHASE G).

Cumulative totals per status only — no per-instrument metric labels. Detailed per-pair results are
returned from :class:`StrategyReadinessSnapshot`, not accumulated here.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.strategy_readiness.models import StrategyReadinessStatus


class ReadinessDiagnostics(BaseModel):
    """Bounded snapshot of cumulative readiness evaluation counters."""

    model_config = ConfigDict(frozen=True)

    evaluations_total: int
    ready_total: int
    warming_up_total: int
    missing_history_total: int
    missing_reference_total: int
    missing_live_data_total: int
    universe_mismatch_total: int
    removed_total: int
    stale_total: int
    last_evaluation_at: datetime | None


_STATUS_FIELD = {
    StrategyReadinessStatus.READY: "ready_total",
    StrategyReadinessStatus.WARMING_UP: "warming_up_total",
    StrategyReadinessStatus.MISSING_HISTORY: "missing_history_total",
    StrategyReadinessStatus.MISSING_REFERENCE: "missing_reference_total",
    StrategyReadinessStatus.MISSING_LIVE_DATA: "missing_live_data_total",
    StrategyReadinessStatus.UNIVERSE_MISMATCH: "universe_mismatch_total",
    StrategyReadinessStatus.REMOVED: "removed_total",
    StrategyReadinessStatus.STALE: "stale_total",
}


class ReadinessMetrics:
    """Mutable cumulative counters (fixed field set; no per-instrument growth)."""

    __slots__ = (
        "evaluations_total",
        "ready_total",
        "warming_up_total",
        "missing_history_total",
        "missing_reference_total",
        "missing_live_data_total",
        "universe_mismatch_total",
        "removed_total",
        "stale_total",
        "last_evaluation_at",
    )

    def __init__(self) -> None:
        self.evaluations_total = 0
        self.ready_total = 0
        self.warming_up_total = 0
        self.missing_history_total = 0
        self.missing_reference_total = 0
        self.missing_live_data_total = 0
        self.universe_mismatch_total = 0
        self.removed_total = 0
        self.stale_total = 0
        self.last_evaluation_at: datetime | None = None

    def record(self, status: StrategyReadinessStatus, now: datetime) -> None:
        """Count one evaluation of the given status."""
        self.evaluations_total += 1
        setattr(self, _STATUS_FIELD[status], getattr(self, _STATUS_FIELD[status]) + 1)
        self.last_evaluation_at = now

    def snapshot(self) -> ReadinessDiagnostics:
        """Freeze the counters into an immutable diagnostics model."""
        return ReadinessDiagnostics(
            evaluations_total=self.evaluations_total,
            ready_total=self.ready_total,
            warming_up_total=self.warming_up_total,
            missing_history_total=self.missing_history_total,
            missing_reference_total=self.missing_reference_total,
            missing_live_data_total=self.missing_live_data_total,
            universe_mismatch_total=self.universe_mismatch_total,
            removed_total=self.removed_total,
            stale_total=self.stale_total,
            last_evaluation_at=self.last_evaluation_at,
        )
