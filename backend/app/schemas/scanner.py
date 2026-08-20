"""API response schemas for the cross-instrument scanner (ADR-012 REST addendum).

Provider-neutral, read-only projections of the in-memory :class:`ScannerSnapshot`. Decimal
ranking values are serialized as exact **strings** (API4) — never through a binary float —
and instruments expose only the canonical ``exchange``/``symbol`` (API5). No directional
field exists, and the API never re-ranks: candidates are emitted in the scanner's order.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict

from app.services.cross_instrument_scanner import ScannerSnapshot


class ScannerCandidateResponse(BaseModel):
    """One ranked candidate as exposed over the API (non-directional)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rank: int
    exchange: str
    symbol: str
    status: str
    ranking_metric_name: str
    ranking_metric_value: str  # exact Decimal string, precision-preserving (API4)


class ScannerSnapshotResponse(BaseModel):
    """The API projection of a cross-instrument :class:`ScannerSnapshot`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: str
    strategy_version: str
    config_version: str
    trading_date: date
    expected_count: int
    evaluated_count: int
    eligible_count: int
    completeness: str
    candidates: tuple[ScannerCandidateResponse, ...]


class ScannerResponse(BaseModel):
    """The scanner endpoint envelope: the current snapshot, or ``null`` when none exists."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: ScannerSnapshotResponse | None


def project_snapshot(snapshot: ScannerSnapshot, *, limit: int | None) -> ScannerSnapshotResponse:
    """Project a scanner snapshot into its API response (rank order preserved; counts intact).

    ``limit`` truncates the emitted candidate list to the top-``limit`` by existing rank; it is
    a response projection only and never alters the snapshot's counts or ranking (API13).
    """
    candidates = snapshot.candidates if limit is None else snapshot.candidates[:limit]
    return ScannerSnapshotResponse(
        strategy_id=snapshot.strategy_id,
        strategy_version=snapshot.strategy_version,
        config_version=snapshot.config_version,
        trading_date=snapshot.trading_date,
        expected_count=snapshot.expected_count,
        evaluated_count=snapshot.evaluated_count,
        eligible_count=snapshot.eligible_count,
        completeness=snapshot.completeness.value,
        candidates=tuple(
            ScannerCandidateResponse(
                rank=candidate.rank,
                exchange=candidate.instrument.exchange,
                symbol=candidate.instrument.symbol,
                status=candidate.status.value,
                ranking_metric_name=candidate.ranking_metric_name,
                ranking_metric_value=str(candidate.ranking_metric_value),
            )
            for candidate in candidates
        ),
    )
