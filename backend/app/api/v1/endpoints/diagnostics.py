"""Read-only diagnostics endpoints (SECTOR-VIEW-1D; market-authority H9C-P4 Gate K).

``GET /diagnostics/sector-shadow`` projects the passive SectorShadowRuntime's existing latest-good
snapshot. ``GET /diagnostics/market-authority`` projects the single-Dhan-owner authority state
(ownership lease, token-mint metadata, ``md:health``, and the B11 readiness verdict) for an
operator. Both are strictly read-only: no sector math, no provider/Dhan calls, no mutation, no
ownership acquisition, no token mint — only bounded reads of existing Redis/runtime state, and never
a secret. Responses are ``Cache-Control: no-store`` (intraday state).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.cache import get_redis
from app.core.config import get_settings
from app.core.lifecycle import ApplicationLifecycle
from app.market_ipc.health import IngestionHealthReader
from app.schemas.market_authority_diagnostics import MarketAuthorityDiagnostics, project_authority
from app.schemas.sector_shadow_diagnostics import (
    SectorShadowDiagnosticsResponse,
    SectorShadowDiagnosticsSource,
    project,
)

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.market_ingestion.ownership import OwnershipSnapshot
    from app.market_ipc.config import MarketIpcConfig
    from app.market_ipc.loss_detection import LossDetectionResult
    from app.market_ipc.state import IngestionHealthState

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
        tick_engine=source.tick_engine_diagnostics(),
        sector=sector,
        limit=limit,
    )


@router.get("/diagnostics/market-authority", summary="Read-only market-provider authority state")
async def get_market_authority_diagnostics(
    request: Request,
    response: Response,
    redis: Annotated[Redis, Depends(get_redis)],
) -> MarketAuthorityDiagnostics:
    """Return the single-Dhan-owner authority state for an operator (read-only, secret-free).

    Reuses existing state only: the ownership lease snapshot, the token-mint metadata, the
    ``md:health`` record, and the composed consumer runtime's B11 authority-readiness verdict. It
    never acquires ownership, mints a token, starts a provider, or contacts Dhan; every read fails
    closed to an unknown/absent projection rather than a 500. ``authority.ready`` is never true on
    missing/stale evidence (the B11 evaluator's own fail-closed semantics).
    """
    response.headers["Cache-Control"] = "no-store"
    settings = get_settings()
    config = settings.market_ipc_config()
    now = datetime.now(UTC)

    ownership = await _read_ownership(redis, settings)
    token_mint = await _read_token_mint(redis, settings)
    health = await IngestionHealthReader(redis, config).read()
    health_stale = _is_health_stale(health, config, now)
    authority = await _read_authority(request)

    return project_authority(
        build_sha=settings.build_sha,
        ownership_enabled=settings.market_ownership_enabled,
        ownership=ownership,
        token_mint=token_mint,
        health=health,
        health_stale=health_stale,
        authority=authority,
    )


async def _read_ownership(redis: Redis, settings: Settings) -> OwnershipSnapshot | None:
    """Read the ownership lease snapshot (read-only); ``None`` on any error (fail closed)."""
    from app.market_ingestion.ownership import RedisOwnershipCoordinator

    try:
        coordinator = RedisOwnershipCoordinator(redis, settings.market_ownership_config())
        return await coordinator.snapshot()
    except (RedisError, ValueError, TypeError):
        return None


async def _read_token_mint(redis: Redis, settings: Settings) -> dict[str, object] | None:
    """Read the token-mint metadata record (read-only); ``None`` when absent/unreadable."""
    try:
        raw = await redis.get(settings.token_mint_config().mint_key)
    except (RedisError, ValueError, TypeError):
        return None
    if raw is None:
        return None
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return record if isinstance(record, dict) else None


async def _read_authority(request: Request) -> LossDetectionResult | None:
    """Evaluate authority readiness via the composed consumer runtime; ``None`` if not composed."""
    runtime = getattr(request.app.state, "market_consumer_runtime", None)
    if runtime is None:
        return None
    try:
        return cast("LossDetectionResult", await runtime.evaluate_authority_readiness())
    except (RedisError, ValueError, TypeError):
        return None


def _is_health_stale(
    health: IngestionHealthState | None, config: MarketIpcConfig, now: datetime
) -> bool:
    """Whether ``md:health`` is absent or older than the reader's staleness deadline."""
    if health is None:
        return True
    return abs((now - health.updated_at).total_seconds()) > config.health_stale_seconds
