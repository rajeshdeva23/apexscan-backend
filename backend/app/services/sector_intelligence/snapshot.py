"""Internal immutable shadow-snapshot contract (SECTOR-VIEW-1B).

Carries only raw SECTOR-3/4 evidence plus completeness/diagnostic counts. Deliberately no
SectorScore, no confidence, no STRONG_* labels, and no LEADER/PARTICIPANT/LAGGARD/DIVERGENT
stock labels — those are calibrated production concepts (SECTOR-5C/5D), not shadow output.
"""

from __future__ import annotations

from datetime import date, datetime

from app.market_intelligence.sector.metrics import SectorMetrics, UniverseProxyMetrics
from app.market_intelligence.sector.models import FrozenModel
from app.market_intelligence.sector.participation import SectorStockRanking
from app.services.sector_intelligence.diagnostics import ShadowDiagnosticsView

SCHEMA_VERSION = "sector-shadow-1"


class SectorShadowSnapshot(FrozenModel):
    """One immutable whole-universe shadow evaluation (raw, un-calibrated).

    ``sector_metrics`` and ``stock_rankings`` are the verbatim SECTOR-3/SECTOR-4 outputs.
    ``universe_proxy`` is ``None`` only before a session trading date is established.
    """

    schema_version: str = SCHEMA_VERSION
    trading_date: date | None
    evaluation_timestamp: datetime

    expected_universe_count: int
    observed_count: int
    complete_count: int
    fresh_count: int
    stale_count: int

    missing_previous_close_count: int
    missing_session_open_count: int
    missing_last_price_count: int
    other_incomplete_count: int

    universe_proxy: UniverseProxyMetrics | None
    sector_metrics: tuple[SectorMetrics, ...]
    stock_rankings: tuple[SectorStockRanking, ...]

    runtime_diagnostics: ShadowDiagnosticsView
