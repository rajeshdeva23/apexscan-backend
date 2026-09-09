"""Per-(instrument, strategy) readiness result models (DECOUPLING PHASE G).

Planning-time readiness: does an (instrument, strategy) pair have every declared input available
before that strategy is allowed to consider the instrument? This is distinct from the live
evaluation-time gate (``strategy_manager.assess_readiness``), which this phase does not touch.

Universe membership != historical completeness != strategy readiness. There is NO global
stock-ready flag; readiness is always per strategy. Every non-READY status carries a
machine-readable reason and never collapses to a bare boolean.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class StrategyReadinessStatus(StrEnum):
    """Deterministic per-(instrument, strategy) readiness status (frozen Phase-G set)."""

    READY = "ready"
    WARMING_UP = "warming_up"
    MISSING_HISTORY = "missing_history"
    MISSING_REFERENCE = "missing_reference"
    MISSING_LIVE_DATA = "missing_live_data"
    UNIVERSE_MISMATCH = "universe_mismatch"
    REMOVED = "removed"
    STALE = "stale"


class ReadinessReasonCode(StrEnum):
    """Machine-readable reason a pair is not READY (or how it resolved)."""

    REMOVED_FROM_UNIVERSE = "removed_from_universe"
    UNIVERSE_VERSION_MISMATCH = "universe_version_mismatch"
    INSUFFICIENT_HISTORY = "insufficient_history"
    PREVIOUS_DAY_MISSING = "previous_day_missing"
    ADJUSTED_HISTORY_UNAVAILABLE = "adjusted_history_unavailable"
    MISSING_PREVIOUS_CLOSE = "missing_previous_close"
    MISSING_SESSION_OPEN = "missing_session_open"
    MISSING_LAST_PRICE = "missing_last_price"
    LIVE_DATA_STALE = "live_data_stale"
    AWAITING_FIRST_OBSERVATION = "awaiting_first_observation"


class ReadinessReason(BaseModel):
    """One bounded, structured reason for a readiness verdict."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: ReadinessReasonCode
    field: str | None = None
    required: str | None = None
    available: str | None = None
    message: str | None = None


class StrategyReadinessResult(BaseModel):
    """Immutable readiness verdict for one (instrument, strategy) at a trading date."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    instrument_identity: str
    strategy_id: str
    universe_version: int
    trading_date: date
    status: StrategyReadinessStatus
    reasons: tuple[ReadinessReason, ...]
    evaluated_at: datetime

    @property
    def is_ready(self) -> bool:
        """Whether the pair is READY."""
        return self.status is StrategyReadinessStatus.READY


class StrategyReadinessSnapshot(BaseModel):
    """Immutable snapshot of readiness across instruments x strategies for a trading date."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    trading_date: date
    universe_version: int
    results: tuple[StrategyReadinessResult, ...]

    @property
    def totals_by_status(self) -> dict[StrategyReadinessStatus, int]:
        """Count of results per status (every status present, zero-filled)."""
        totals = {status: 0 for status in StrategyReadinessStatus}
        for result in self.results:
            totals[result.status] += 1
        return totals

    def ready_pairs(self) -> tuple[tuple[str, str], ...]:
        """(instrument_identity, strategy_id) pairs that are READY."""
        return tuple(
            (result.instrument_identity, result.strategy_id)
            for result in self.results
            if result.is_ready
        )
