"""Read-only sector-shadow diagnostics endpoint (SECTOR-VIEW-1D).

``GET /diagnostics/sector-shadow`` projects the passive SectorShadowRuntime's existing
latest-good snapshot + bounded counters, plus the provider's live-frame decode diagnostics.
It performs zero sector math, zero provider/Dhan/DB/Redis calls, and no mutation — it reads the
lifecycle-owned runtime via the narrow :class:`SectorShadowDiagnosticsSource` seam. Responses are
``Cache-Control: no-store`` (intraday state).
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.core.lifecycle import ApplicationLifecycle
from app.schemas.sector_shadow_diagnostics import (
    SectorShadowDiagnosticsResponse,
    SectorShadowDiagnosticsSource,
    project,
)

router = APIRouter(tags=["diagnostics"])

_NO_STORE = {"Cache-Control": "no-store"}


def _shadow_source(request: Request) -> SectorShadowDiagnosticsSource | None:
    """Return the lifecycle-owned shadow-diagnostics read seam, or ``None`` when unavailable."""
    lifecycle = cast(ApplicationLifecycle, request.app.state.lifecycle)
    provider = lifecycle.provider
    return provider if isinstance(provider, SectorShadowDiagnosticsSource) else None


@router.get("/diagnostics/sector-shadow", summary="Read-only sector-shadow diagnostics")
async def get_sector_shadow_diagnostics(
    request: Request,
    response: Response,
    sector: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limit: Annotated[int | None, Query(ge=1, le=50)] = None,
) -> SectorShadowDiagnosticsResponse:
    """Return the current sector-shadow diagnostics (read-only; no evaluation triggered).

    Args:
        request: Used to reach the lifecycle-owned shadow read seam.
        response: Used to set the no-store cache header.
        sector: Optional sector_id filter for the bounded stock-participation sample.
        limit: Optional cap on the stock-participation sample (1..50).

    Returns:
        ``SectorShadowDiagnosticsResponse`` projecting the runtime's latest-good state.

    Raises:
        HTTPException: ``503`` when the runtime is unavailable/not started.
    """
    response.headers["Cache-Control"] = "no-store"
    source = _shadow_source(request)
    if source is None or not source.sector_shadow_read_available():
        raise HTTPException(status_code=503, detail="shadow runtime unavailable", headers=_NO_STORE)
    return project(
        snapshot=source.sector_shadow_snapshot(),
        diagnostics=source.sector_shadow_diagnostics(),
        live_feed=source.live_feed_decode_diagnostics(),
        sector=sector,
        limit=limit,
    )
