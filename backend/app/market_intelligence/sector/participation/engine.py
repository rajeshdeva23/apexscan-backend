"""Pure, deterministic stock-participation ranking (SECTOR-4).

Builds raw stock-vs-sector / stock-vs-universe evidence and a within-sector directional
ranking from a SECTOR-3 ``SectorMetrics`` context plus the stock observations. No network,
DB, Redis, EventBus, provider, filesystem, wall-clock, or global state; same input → same
output. All thresholds/labels are deferred to SECTOR-5 — this layer only exposes raw math.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

from app.market_intelligence.sector.metrics import (
    CalculationPolicy,
    ConstituentDirection,
    ConstituentObservation,
    RawSectorDirection,
    SectorMetrics,
    calculate_constituent_metrics,
)
from app.market_intelligence.sector.participation.models import (
    DuplicateRankedConstituentError,
    SectorStockRanking,
    StockExclusionReason,
    StockSectorAlignment,
    StockSectorContextMismatchError,
    StockSectorMetrics,
)

_DIRECTIONAL = frozenset({RawSectorDirection.BULLISH, RawSectorDirection.BEARISH})


def _alignment(
    stock_direction: ConstituentDirection, sector_direction: RawSectorDirection
) -> StockSectorAlignment:
    if sector_direction is RawSectorDirection.MIXED:
        return StockSectorAlignment.MIXED_CONTEXT
    if sector_direction is RawSectorDirection.INSUFFICIENT_DATA:
        return StockSectorAlignment.INSUFFICIENT_DATA
    if sector_direction is RawSectorDirection.NEUTRAL:
        return StockSectorAlignment.NEUTRAL
    if stock_direction is ConstituentDirection.UNCHANGED:
        return StockSectorAlignment.NEUTRAL
    advancing = stock_direction is ConstituentDirection.ADVANCING
    bullish = sector_direction is RawSectorDirection.BULLISH
    return StockSectorAlignment.ALIGNED if advancing == bullish else StockSectorAlignment.OPPOSED


def _directional_strength(
    intraday_return: Decimal, sector_direction: RawSectorDirection
) -> Decimal | None:
    if sector_direction is RawSectorDirection.BULLISH:
        return intraday_return
    if sector_direction is RawSectorDirection.BEARISH:
        return -intraday_return
    return None


def _robust_relative_magnitude(
    intraday_return: Decimal, sector_median: Decimal | None, mad: Decimal | None
) -> Decimal | None:
    if sector_median is None or mad is None or mad == 0:
        return None
    return (intraday_return - sector_median) / mad


def _relative(value: Decimal, benchmark: Decimal | None) -> Decimal | None:
    return None if benchmark is None else value - benchmark


def _validate(
    sector_metrics: SectorMetrics, observations: Sequence[ConstituentObservation]
) -> None:
    sid = sector_metrics.sector_id
    seen: set[str] = set()
    for observation in observations:
        if observation.identity in seen:
            raise DuplicateRankedConstituentError(
                f"duplicate ranked identity: {observation.identity}"
            )
        seen.add(observation.identity)
        if observation.sector_id != sid:
            raise StockSectorContextMismatchError(
                f"{observation.identity} sector {observation.sector_id} != {sid}"
            )
        if observation.trading_date != sector_metrics.trading_date:
            raise StockSectorContextMismatchError(
                f"{observation.identity} trading_date != {sector_metrics.trading_date}"
            )


def _base_metrics(
    observation: ConstituentObservation,
    sector_metrics: SectorMetrics,
    policy: CalculationPolicy,
    *,
    eligible: bool,
) -> StockSectorMetrics:
    cm = calculate_constituent_metrics(observation, policy.direction_epsilon)
    sector_median = sector_metrics.median_intraday_return
    proxy = sector_metrics.universe_proxy_intraday_return
    direction = sector_metrics.raw_direction
    return StockSectorMetrics(
        identity=cm.identity,
        sector_id=sector_metrics.sector_id,
        trading_date=sector_metrics.trading_date,
        evaluation_timestamp=sector_metrics.evaluation_timestamp,
        stock_intraday_return=cm.intraday_return,
        stock_overnight_return=cm.overnight_return,
        stock_total_return=cm.total_return,
        stock_direction=cm.direction,
        sector_raw_direction=direction,
        sector_median_intraday_return=sector_median,
        universe_proxy_intraday_return=proxy,
        stock_vs_sector=_relative(cm.intraday_return, sector_median),
        stock_vs_universe=_relative(cm.intraday_return, proxy),
        sector_alignment=_alignment(cm.direction, direction),
        directional_strength=_directional_strength(cm.intraday_return, direction),
        robust_relative_magnitude=_robust_relative_magnitude(
            cm.intraday_return, sector_median, sector_metrics.dispersion.mad_intraday_return
        ),
        within_sector_rank=None,
        within_sector_percentile=None,
        eligible=eligible,
        exclusion_reason=None if eligible else StockExclusionReason.STALE,
    )


def calculate_stock_sector_metrics(
    observation: ConstituentObservation,
    sector_metrics: SectorMetrics,
    policy: CalculationPolicy,
) -> StockSectorMetrics:
    """Return one stock's raw sector/universe-relative evidence (no ranking assigned).

    Fails closed on sector/trading-date mismatch. ``eligible`` reflects freshness against
    ``sector_metrics.evaluation_timestamp``; rank/percentile are only assigned by
    :func:`rank_sector_constituents` over the full eligible set.
    """
    if observation.sector_id != sector_metrics.sector_id:
        raise StockSectorContextMismatchError(
            f"{observation.identity} sector {observation.sector_id} != {sector_metrics.sector_id}"
        )
    if observation.trading_date != sector_metrics.trading_date:
        raise StockSectorContextMismatchError(
            f"{observation.identity} trading_date != {sector_metrics.trading_date}"
        )
    age = sector_metrics.evaluation_timestamp - observation.observation_timestamp
    eligible = timedelta(0) <= age <= policy.freshness_limit
    return _base_metrics(observation, sector_metrics, policy, eligible=eligible)


def _apply_ranking(eligible: list[StockSectorMetrics]) -> tuple[StockSectorMetrics, ...]:
    """Assign competition rank + percentile by directional strength, then order the set.

    Ties share an analytical rank and percentile; canonical identity is the final,
    display-only tiebreaker. Percentile is 1.0 (strongest) → 0.0 (weakest); None for N<2.
    """
    strengths = [s.directional_strength for s in eligible]
    count = len(eligible)
    ranked: list[StockSectorMetrics] = []
    for stock in eligible:
        strength = stock.directional_strength
        rank = 1 + sum(
            1
            for other in strengths
            if other is not None and strength is not None and other > strength
        )
        percentile = None if count < 2 else Decimal(count - rank) / Decimal(count - 1)
        ranked.append(
            stock.model_copy(
                update={"within_sector_rank": rank, "within_sector_percentile": percentile}
            )
        )
    ranked.sort(key=lambda s: (-(s.directional_strength or Decimal(0)), s.identity))
    return tuple(ranked)


def rank_sector_constituents(
    sector_metrics: SectorMetrics,
    observations: Sequence[ConstituentObservation],
    policy: CalculationPolicy,
) -> SectorStockRanking:
    """Rank a sector's constituents relative to the sector and F&O-universe proxy.

    ``observations`` must match ``sector_metrics`` sector_id/trading_date and be unique
    (fail-closed). Freshness uses ``sector_metrics.evaluation_timestamp``; stale
    observations are excluded (never ranked, never treated as unchanged). Directional
    rank/percentile are produced only when the sector raw direction is BULLISH/BEARISH.
    """
    _validate(sector_metrics, observations)
    limit = policy.freshness_limit
    eval_at = sector_metrics.evaluation_timestamp
    fresh: list[ConstituentObservation] = []
    stale: list[ConstituentObservation] = []
    for observation in observations:
        age = eval_at - observation.observation_timestamp
        (fresh if timedelta(0) <= age <= limit else stale).append(observation)

    excluded = tuple(_base_metrics(o, sector_metrics, policy, eligible=False) for o in stale)
    eligible = [_base_metrics(o, sector_metrics, policy, eligible=True) for o in fresh]
    directional = sector_metrics.raw_direction in _DIRECTIONAL
    if directional:
        ranked = _apply_ranking(eligible)
    else:
        ranked = tuple(sorted(eligible, key=lambda s: s.identity))

    return SectorStockRanking(
        sector_id=sector_metrics.sector_id,
        trading_date=sector_metrics.trading_date,
        evaluation_timestamp=eval_at,
        sector_raw_direction=sector_metrics.raw_direction,
        directional_ranking_available=directional,
        eligible_count=len(eligible),
        ranked_stocks=ranked,
        excluded_stocks=tuple(sorted(excluded, key=lambda s: s.identity)),
    )
