"""Offline whole-universe shadow-validation harness for SECTOR-2/3/4 (SECTOR-VALIDATION-1).

Evidence tooling only — NOT imported by application startup, the provider runtime, the Market
Engine, the EventBus, or strategies. It *orchestrates* the merged pure functions
(``MembershipResolver`` → ``calculate_universe_proxy`` → ``calculate_sector_metrics`` →
``rank_sector_constituents``) over a snapshot of the whole F&O universe; it re-implements no
metric. Deterministic, network-free, no wall-clock. No SectorScore, confidence, thresholds,
or classifications — those remain SECTOR-5.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import Field

from app.market_intelligence.sector.membership import MembershipResolver
from app.market_intelligence.sector.metrics import (
    CalculationPolicy,
    ConstituentObservation,
    DuplicateConstituentError,
    MixedTradingDateError,
    RawSectorDirection,
    SectorMetrics,
    calculate_sector_metrics,
    calculate_universe_proxy,
)
from app.market_intelligence.sector.models import FrozenModel
from app.market_intelligence.sector.participation import (
    SectorStockRanking,
    rank_sector_constituents,
)

SCHEMA_VERSION = "sector-validation-1"


class ValidationObservation(FrozenModel):
    """One constituent snapshot (sector resolved by the harness from SECTOR-2 membership)."""

    identity: str = Field(min_length=1)
    trading_date: date
    observation_timestamp: datetime
    last_price: Decimal = Field(gt=0)
    previous_close: Decimal = Field(gt=0)
    session_open: Decimal = Field(gt=0)


class SectorEvaluation(FrozenModel):
    """A sector's SECTOR-3 metrics plus its SECTOR-4 stock ranking for one snapshot."""

    sector_id: str
    metrics: SectorMetrics
    ranking: SectorStockRanking


class UniverseEvaluation(FrozenModel):
    """Whole-universe shadow evaluation at one instant (raw evidence, un-calibrated)."""

    trading_date: date
    evaluation_timestamp: datetime
    expected_universe_count: int
    observed_count: int
    mapped_count: int
    unmapped_identities: tuple[str, ...]
    valid_count: int
    stale_count: int
    universe_proxy_intraday_return: Decimal | None
    universe_proxy_valid_count: int
    sectors: tuple[SectorEvaluation, ...]


def _validate_inputs(observations: list[ValidationObservation], trading_date: date) -> None:
    seen: set[str] = set()
    for observation in observations:
        if observation.identity in seen:
            raise DuplicateConstituentError(f"duplicate identity: {observation.identity}")
        seen.add(observation.identity)
        if observation.trading_date != trading_date:
            raise MixedTradingDateError(f"{observation.identity} trading_date != {trading_date}")


def _as_constituent(observation: ValidationObservation, sector_id: str) -> ConstituentObservation:
    return ConstituentObservation(
        identity=observation.identity,
        sector_id=sector_id,
        trading_date=observation.trading_date,
        observation_timestamp=observation.observation_timestamp,
        last_price=observation.last_price,
        previous_close=observation.previous_close,
        session_open=observation.session_open,
    )


def evaluate_universe(
    observations: list[ValidationObservation],
    resolver: MembershipResolver,
    policy: CalculationPolicy,
    trading_date: date,
    evaluation_time: datetime,
) -> UniverseEvaluation:
    """Evaluate every primary sector + its stock ranking from a whole-universe snapshot.

    Fails closed on duplicate identity or mixed trading date. Unmapped identities are
    excluded and reported (never guessed). Reuses SECTOR-3/4 math verbatim so validation and
    production cannot diverge.
    """
    _validate_inputs(observations, trading_date)
    by_sector: dict[str, list[ConstituentObservation]] = {}
    unmapped: list[str] = []
    for observation in observations:
        primary = resolver.resolve_primary(observation.identity, on=trading_date)
        if primary is None:
            unmapped.append(observation.identity)
            continue
        by_sector.setdefault(primary, []).append(_as_constituent(observation, primary))

    all_constituents = [c for group in by_sector.values() for c in group]
    proxy = calculate_universe_proxy(all_constituents, policy, trading_date, evaluation_time)

    sectors: list[SectorEvaluation] = []
    stale = 0
    for sector_id in resolver.all_primary_sectors():
        expected = frozenset(resolver.members_of_primary_sector(sector_id, on=trading_date))
        sector_obs = by_sector.get(sector_id, [])
        metrics = calculate_sector_metrics(
            sector_id, expected, sector_obs, policy, trading_date, evaluation_time, proxy
        )
        ranking = rank_sector_constituents(metrics, sector_obs, policy)
        stale += metrics.stale_count
        sectors.append(SectorEvaluation(sector_id=sector_id, metrics=metrics, ranking=ranking))

    valid = sum(s.metrics.valid_count for s in sectors)
    return UniverseEvaluation(
        trading_date=trading_date,
        evaluation_timestamp=evaluation_time,
        expected_universe_count=sum(
            len(resolver.members_of_primary_sector(s)) for s in resolver.all_primary_sectors()
        ),
        observed_count=len(observations),
        mapped_count=len(all_constituents),
        unmapped_identities=tuple(sorted(unmapped)),
        valid_count=valid,
        stale_count=stale,
        universe_proxy_intraday_return=proxy.median_intraday_return,
        universe_proxy_valid_count=proxy.valid_count,
        sectors=tuple(sectors),
    )


def to_artifact(evaluation: UniverseEvaluation) -> dict[str, object]:
    """Render the SECTOR-VALIDATION-1 evidence artifact (sanitized; no SectorScore/confidence)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "trading_date": evaluation.trading_date.isoformat(),
        "evaluation_timestamp": evaluation.evaluation_timestamp.isoformat(),
        "expected_universe_count": evaluation.expected_universe_count,
        "observed_count": evaluation.observed_count,
        "mapped_count": evaluation.mapped_count,
        "valid_count": evaluation.valid_count,
        "stale_count": evaluation.stale_count,
        "unmapped_identities": list(evaluation.unmapped_identities),
        "universe_proxy_intraday_return": _s(evaluation.universe_proxy_intraday_return),
        "sectors": [
            {
                "sector_id": s.sector_id,
                "member_count": s.metrics.expected_count,
                "valid_count": s.metrics.valid_count,
                "coverage_ratio": _s(s.metrics.coverage_ratio),
                "median_intraday_return": _s(s.metrics.median_intraday_return),
                "net_breadth": _s(s.metrics.breadth.net_breadth),
                "mad_intraday_return": _s(s.metrics.dispersion.mad_intraday_return),
                "iqr_intraday_return": _s(s.metrics.dispersion.iqr_intraday_return),
                "relative_strength": _s(s.metrics.relative_strength),
                "raw_direction": s.metrics.raw_direction.value,
                "directional_ranking_available": s.ranking.directional_ranking_available,
                "stocks": [
                    {
                        "identity": st.identity,
                        "intraday_return": _s(st.stock_intraday_return),
                        "stock_vs_sector": _s(st.stock_vs_sector),
                        "stock_vs_universe": _s(st.stock_vs_universe),
                        "alignment": st.sector_alignment.value,
                        "within_sector_rank": st.within_sector_rank,
                        "within_sector_percentile": _s(st.within_sector_percentile),
                        "eligible": st.eligible,
                        "exclusion_reason": st.exclusion_reason.value
                        if st.exclusion_reason
                        else None,
                    }
                    for st in (*s.ranking.ranked_stocks, *s.ranking.excluded_stocks)
                ],
            }
            for s in evaluation.sectors
        ],
    }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def raw_directions(evaluation: UniverseEvaluation) -> dict[str, RawSectorDirection]:
    """Convenience: sector_id -> raw direction for quick inspection."""
    return {s.sector_id: s.metrics.raw_direction for s in evaluation.sectors}
