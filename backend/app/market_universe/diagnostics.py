"""Bounded, credential-free universe diagnostics (DECOUPLING PHASE E).

Aggregate counts only — never per-instrument metric labels. Detailed unresolved instruments are
returned from the :class:`ResolutionResult` (an admin/service surface), not emitted here.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.market_universe.resolver import ResolutionResult
from app.market_universe.snapshot import UniverseSnapshot, diff_snapshots


class UniverseDiagnostics(BaseModel):
    """Bounded snapshot of one resolution plus cumulative resolver counters."""

    model_config = ConfigDict(frozen=True)

    resolution_attempts: int
    resolution_successes: int
    resolution_failures: int
    current_active_version: int | None
    candidate_version: int
    instrument_count: int
    added_count: int
    removed_count: int
    mapping_changed_count: int
    sector_changed_count: int
    unresolved_mapping_count: int
    unclassified_sector_count: int
    last_resolution_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None


class UniverseMetrics:
    """Mutable cumulative resolver counters (fixed field set; no per-instrument growth)."""

    __slots__ = (
        "attempts",
        "successes",
        "failures",
        "last_resolution_at",
        "last_success_at",
        "last_failure_at",
    )

    def __init__(self) -> None:
        self.attempts = 0
        self.successes = 0
        self.failures = 0
        self.last_resolution_at: datetime | None = None
        self.last_success_at: datetime | None = None
        self.last_failure_at: datetime | None = None

    def record_attempt(self, now: datetime) -> None:
        """Count a resolution attempt."""
        self.attempts += 1
        self.last_resolution_at = now

    def record_success(self, now: datetime) -> None:
        """Count a successful resolution."""
        self.successes += 1
        self.last_success_at = now

    def record_failure(self, now: datetime) -> None:
        """Count a failed resolution."""
        self.failures += 1
        self.last_failure_at = now


def build_universe_diagnostics(
    *,
    metrics: UniverseMetrics,
    result: ResolutionResult,
    active: UniverseSnapshot | None,
) -> UniverseDiagnostics:
    """Assemble bounded diagnostics for a resolution against the current active snapshot."""
    candidate = result.candidate
    diff = diff_snapshots(active, candidate) if active is not None else None
    return UniverseDiagnostics(
        resolution_attempts=metrics.attempts,
        resolution_successes=metrics.successes,
        resolution_failures=metrics.failures,
        current_active_version=active.universe_version if active is not None else None,
        candidate_version=candidate.universe_version,
        instrument_count=len(candidate.instruments),
        added_count=len(diff.added) if diff else len(candidate.instruments),
        removed_count=len(diff.removed) if diff else 0,
        mapping_changed_count=len(diff.mapping_changed) if diff else 0,
        sector_changed_count=len(diff.sector_changed) if diff else 0,
        unresolved_mapping_count=len(result.unresolved),
        unclassified_sector_count=len(result.unclassified_sector),
        last_resolution_at=metrics.last_resolution_at,
        last_success_at=metrics.last_success_at,
        last_failure_at=metrics.last_failure_at,
    )
