"""Generic cross-instrument scanner REST endpoint (ADR-012 REST addendum).

``GET /scanners/{strategy_id}`` returns the current in-memory ranked snapshot for any
scanner-enabled strategy — provider-neutral, read-only, and strategy-generic (no per-strategy
route or branch). It performs zero provider/historical/evaluation work: it reads the
lifecycle-owned scanner via the narrow :class:`ScannerSnapshotSource` seam and projects the
snapshot (rank order preserved). Responses are ``Cache-Control: no-store`` (intraday state).
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.core.lifecycle import ApplicationLifecycle
from app.schemas.scanner import ScannerResponse, project_snapshot
from app.services.cross_instrument_scanner import ScannerSnapshotSource

router = APIRouter(tags=["scanners"])

_NO_STORE = {"Cache-Control": "no-store"}


def _scanner_source(request: Request) -> ScannerSnapshotSource | None:
    """Return the lifecycle-owned scanner read seam, or ``None`` when unavailable.

    Reaches the already-composed provider dependency through the application lifecycle and
    narrows it to the read-only scanner capability. It constructs no runtime, provider, or
    scanner and reads no provider identity.
    """
    lifecycle = cast(ApplicationLifecycle, request.app.state.lifecycle)
    provider = lifecycle.provider
    return provider if isinstance(provider, ScannerSnapshotSource) else None


@router.get("/scanners/{strategy_id}", summary="Current cross-instrument scanner snapshot")
async def get_scanner_snapshot(
    strategy_id: str,
    request: Request,
    response: Response,
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
) -> ScannerResponse:
    """Return the current ranked snapshot for ``strategy_id`` (rank order preserved).

    Args:
        strategy_id: The scanner-enabled strategy id (e.g. ``narrow_cpr``).
        request: The request, used to reach the lifecycle-owned scanner read seam.
        response: The response, used to set the no-store cache header.
        limit: Optional top-N projection over the existing rank order (1..500); counts and
            ranking are unaffected.

    Returns:
        ``ScannerResponse`` with the projected snapshot, or ``snapshot=null`` when the
        strategy is scanner-enabled but has no snapshot yet.

    Raises:
        HTTPException: ``503`` when the scanner runtime is unavailable/not started; ``404``
            when ``strategy_id`` is not scanner-enabled.
    """
    response.headers["Cache-Control"] = "no-store"
    source = _scanner_source(request)
    if source is None or not source.scanner_read_available():
        raise HTTPException(
            status_code=503, detail="scanner runtime unavailable", headers=_NO_STORE
        )
    if strategy_id not in source.scannable_strategy_ids():
        raise HTTPException(status_code=404, detail="unknown scanner strategy", headers=_NO_STORE)
    snapshot = source.scanner_snapshot(strategy_id)
    projected = project_snapshot(snapshot, limit=limit) if snapshot is not None else None
    return ScannerResponse(snapshot=projected)
