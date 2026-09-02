"""Immutable stock-participation vocabulary and outputs (SECTOR-4).

Raw, un-calibrated stock-vs-sector / stock-vs-universe evidence on top of SECTOR-2
membership and SECTOR-3 metrics. No StockParticipationScore, no LEADER/PARTICIPANT/
LAGGARD/DIVERGENT thresholds, no confidence — those are SECTOR-5. Returns are Decimal
ratios (``0.01`` == 1%). ``within_sector_rank`` == 1 does NOT mean "production LEADER".
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from app.market_intelligence.sector.metrics import ConstituentDirection, RawSectorDirection
from app.market_intelligence.sector.models import FrozenModel


class StockRankingError(ValueError):
    """Base for SECTOR-4 stock-ranking domain errors (fail-closed)."""


class StockSectorContextMismatchError(StockRankingError):
    """An observation's sector_id/trading_date disagrees with the SectorMetrics context."""


class DuplicateRankedConstituentError(StockRankingError):
    """The same instrument identity appears more than once in a ranking input."""


class InvalidSectorRankingInputError(StockRankingError):
    """The ranking inputs are structurally invalid."""


class StockSectorAlignment(StrEnum):
    """Raw directional relationship of a stock to its sector's raw direction.

    ``NEUTRAL`` means no directional alignment is established — either the sector is
    NEUTRAL or the stock is UNCHANGED. ``MIXED_CONTEXT`` / ``INSUFFICIENT_DATA`` mirror
    the sector's raw direction (no alignment can be asserted).
    """

    ALIGNED = "aligned"
    OPPOSED = "opposed"
    NEUTRAL = "neutral"
    MIXED_CONTEXT = "mixed_context"
    INSUFFICIENT_DATA = "insufficient_data"


class StockExclusionReason(StrEnum):
    """Why a provided observation is not part of the active (eligible) ranking."""

    STALE = "stale"


class StockSectorMetrics(FrozenModel):
    """Raw per-stock evidence relative to its sector and the F&O universe proxy.

    Absolute (``stock_intraday_return``), sector-relative (``stock_vs_sector``), and
    universe-relative (``stock_vs_universe``) behaviour are kept as three independent
    facts. ``directional_strength``/``within_sector_rank``/``within_sector_percentile`` are
    populated only when the sector has a directional raw state (BULLISH/BEARISH).
    """

    identity: str
    sector_id: str
    trading_date: date
    evaluation_timestamp: datetime
    stock_intraday_return: Decimal
    stock_overnight_return: Decimal
    stock_total_return: Decimal
    stock_direction: ConstituentDirection
    sector_raw_direction: RawSectorDirection
    sector_median_intraday_return: Decimal | None
    universe_proxy_intraday_return: Decimal | None
    stock_vs_sector: Decimal | None
    stock_vs_universe: Decimal | None
    sector_alignment: StockSectorAlignment
    directional_strength: Decimal | None
    robust_relative_magnitude: Decimal | None
    within_sector_rank: int | None
    within_sector_percentile: Decimal | None
    eligible: bool
    exclusion_reason: StockExclusionReason | None


class SectorStockRanking(FrozenModel):
    """Immutable within-sector stock ranking for one sector at one evaluation instant.

    ``directional_ranking_available`` is False when the sector raw direction is
    NEUTRAL/MIXED/INSUFFICIENT_DATA — eligible stocks still carry absolute/relative
    metrics, but no directional rank/percentile is asserted (deterministic identity order).
    """

    sector_id: str
    trading_date: date
    evaluation_timestamp: datetime
    sector_raw_direction: RawSectorDirection
    directional_ranking_available: bool
    eligible_count: int
    ranked_stocks: tuple[StockSectorMetrics, ...]
    excluded_stocks: tuple[StockSectorMetrics, ...]
