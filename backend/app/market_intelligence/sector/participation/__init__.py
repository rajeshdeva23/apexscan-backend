"""Pure stock-participation and within-sector ranking foundation (SECTOR-4, ADR-016).

Raw stock-vs-sector / stock-vs-universe evidence and a deterministic within-sector
directional ranking over SECTOR-2 membership + SECTOR-3 metrics. No StockParticipationScore,
no LEADER/PARTICIPANT/LAGGARD/DIVERGENT thresholds, no confidence, no live wiring.
"""

from app.market_intelligence.sector.participation.engine import (
    calculate_stock_sector_metrics,
    rank_sector_constituents,
)
from app.market_intelligence.sector.participation.models import (
    DuplicateRankedConstituentError,
    InvalidSectorRankingInputError,
    SectorStockRanking,
    StockExclusionReason,
    StockRankingError,
    StockSectorAlignment,
    StockSectorContextMismatchError,
    StockSectorMetrics,
)

__all__ = [
    "DuplicateRankedConstituentError",
    "InvalidSectorRankingInputError",
    "SectorStockRanking",
    "StockExclusionReason",
    "StockRankingError",
    "StockSectorAlignment",
    "StockSectorContextMismatchError",
    "StockSectorMetrics",
    "calculate_stock_sector_metrics",
    "rank_sector_constituents",
]
