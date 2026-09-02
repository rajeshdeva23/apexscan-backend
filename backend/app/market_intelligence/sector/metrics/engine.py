"""Pure, deterministic sector metric calculations (SECTOR-3).

No network, DB, Redis, EventBus, provider, filesystem, wall-clock, or global state.
Every function is a total function of its arguments: same input, same output. Freshness
is decided from supplied ``evaluation_time``/``freshness_limit``, never ``datetime.now()``.
Returns are Decimal ratios (``0.01`` == 1%); intraday (since-open) movement drives
direction, overnight movement is context only (SECTOR-1).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum

from app.market_intelligence.sector.metrics import statistics
from app.market_intelligence.sector.metrics.models import (
    CalculationPolicy,
    ConstituentDirection,
    ConstituentMetrics,
    ConstituentObservation,
    DuplicateConstituentError,
    MixedTradingDateError,
    RawSectorDirection,
    SectorBreadth,
    SectorDispersion,
    SectorMembershipMismatchError,
    SectorMetrics,
    UniverseProxyMetrics,
)


class _Tilt(Enum):
    BULL = 1
    NEUTRAL = 0
    BEAR = -1


def _tilt(value: Decimal | None, epsilon: Decimal) -> _Tilt:
    if value is None:
        return _Tilt.NEUTRAL
    if value > epsilon:
        return _Tilt.BULL
    if value < -epsilon:
        return _Tilt.BEAR
    return _Tilt.NEUTRAL


def _ratio(numerator: int, denominator: int) -> Decimal | None:
    if denominator == 0:
        return None
    return Decimal(numerator) / Decimal(denominator)


def calculate_constituent_metrics(
    observation: ConstituentObservation, direction_epsilon: Decimal
) -> ConstituentMetrics:
    """Decompose one observation into overnight/intraday/total returns and a direction."""
    overnight = (observation.session_open - observation.previous_close) / observation.previous_close
    intraday = (observation.last_price - observation.session_open) / observation.session_open
    total = (observation.last_price - observation.previous_close) / observation.previous_close
    tilt = _tilt(intraday, direction_epsilon)
    direction = {
        _Tilt.BULL: ConstituentDirection.ADVANCING,
        _Tilt.BEAR: ConstituentDirection.DECLINING,
        _Tilt.NEUTRAL: ConstituentDirection.UNCHANGED,
    }[tilt]
    return ConstituentMetrics(
        identity=observation.identity,
        sector_id=observation.sector_id,
        overnight_return=overnight,
        intraday_return=intraday,
        total_return=total,
        direction=direction,
    )


def _reject_duplicates(observations: Iterable[ConstituentObservation]) -> None:
    seen: set[str] = set()
    for observation in observations:
        if observation.identity in seen:
            raise DuplicateConstituentError(
                f"duplicate constituent identity: {observation.identity}"
            )
        seen.add(observation.identity)


def _is_fresh(
    observation: ConstituentObservation, evaluation_time: datetime, limit: timedelta
) -> bool:
    age = evaluation_time - observation.observation_timestamp
    return timedelta(0) <= age <= limit


def _breadth(constituents: Sequence[ConstituentMetrics]) -> SectorBreadth:
    valid = len(constituents)
    advancing = sum(1 for c in constituents if c.direction is ConstituentDirection.ADVANCING)
    declining = sum(1 for c in constituents if c.direction is ConstituentDirection.DECLINING)
    unchanged = valid - advancing - declining
    return SectorBreadth(
        valid_count=valid,
        advance_count=advancing,
        decline_count=declining,
        unchanged_count=unchanged,
        advance_ratio=_ratio(advancing, valid),
        decline_ratio=_ratio(declining, valid),
        unchanged_ratio=_ratio(unchanged, valid),
        net_breadth=_ratio(advancing - declining, valid),
    )


def _agreement(median_tilt: _Tilt, breadth: SectorBreadth) -> Decimal | None:
    return {
        _Tilt.BULL: breadth.advance_ratio,
        _Tilt.BEAR: breadth.decline_ratio,
        _Tilt.NEUTRAL: breadth.unchanged_ratio,
    }[median_tilt]


def _participation(
    median_tilt: _Tilt,
    constituents: Sequence[ConstituentMetrics],
    epsilon: Decimal,
) -> tuple[int, Decimal | None]:
    if median_tilt is _Tilt.BULL:
        count = sum(1 for c in constituents if c.intraday_return > epsilon)
    elif median_tilt is _Tilt.BEAR:
        count = sum(1 for c in constituents if c.intraday_return < -epsilon)
    else:
        count = 0
    return count, _ratio(count, len(constituents))


def _raw_direction(
    median_tilt: _Tilt,
    breadth_tilt: _Tilt,
    valid_count: int,
    coverage_ratio: Decimal,
    policy: CalculationPolicy,
) -> RawSectorDirection:
    if valid_count == 0 or valid_count < policy.minimum_valid_count:
        return RawSectorDirection.INSUFFICIENT_DATA
    if policy.minimum_coverage_ratio is not None and coverage_ratio < policy.minimum_coverage_ratio:
        return RawSectorDirection.INSUFFICIENT_DATA
    if median_tilt is _Tilt.BULL and breadth_tilt is _Tilt.BULL:
        return RawSectorDirection.BULLISH
    if median_tilt is _Tilt.BEAR and breadth_tilt is _Tilt.BEAR:
        return RawSectorDirection.BEARISH
    if median_tilt is _Tilt.NEUTRAL and breadth_tilt is _Tilt.NEUTRAL:
        return RawSectorDirection.NEUTRAL
    return RawSectorDirection.MIXED


def _validate_scope(
    sector_id: str,
    expected: frozenset[str],
    observations: Sequence[ConstituentObservation],
    trading_date: date,
) -> None:
    _reject_duplicates(observations)
    for observation in observations:
        if observation.sector_id != sector_id:
            raise SectorMembershipMismatchError(
                f"{observation.identity} is {observation.sector_id}, not {sector_id}"
            )
        if observation.identity not in expected:
            raise SectorMembershipMismatchError(
                f"{observation.identity} not in {sector_id} membership"
            )
        if observation.trading_date != trading_date:
            raise MixedTradingDateError(f"{observation.identity} trading_date != {trading_date}")


def calculate_universe_proxy(
    observations: Sequence[ConstituentObservation],
    policy: CalculationPolicy,
    trading_date: date,
    evaluation_time: datetime,
) -> UniverseProxyMetrics:
    """Equal-weight median intraday return over valid, fresh eligible F&O observations.

    Uses every fresh observation regardless of sector coverage. Duplicate identities and
    mixed trading dates fail closed. This is an F&O-universe proxy, not a NIFTY return.
    """
    _reject_duplicates(observations)
    for observation in observations:
        if observation.trading_date != trading_date:
            raise MixedTradingDateError(f"{observation.identity} trading_date != {trading_date}")
    fresh = [o for o in observations if _is_fresh(o, evaluation_time, policy.freshness_limit)]
    intraday = [
        calculate_constituent_metrics(o, policy.direction_epsilon).intraday_return for o in fresh
    ]
    return UniverseProxyMetrics(
        valid_count=len(fresh), median_intraday_return=statistics.median(intraday)
    )


def calculate_relative_strength(
    sector_median_intraday: Decimal | None, universe_proxy_intraday: Decimal | None
) -> Decimal | None:
    """Sector median intraday minus universe-proxy median intraday, or None if unavailable."""
    if sector_median_intraday is None or universe_proxy_intraday is None:
        return None
    return sector_median_intraday - universe_proxy_intraday


def calculate_sector_metrics(
    sector_id: str,
    expected_identities: frozenset[str],
    observations: Sequence[ConstituentObservation],
    policy: CalculationPolicy,
    trading_date: date,
    evaluation_time: datetime,
    universe_proxy: UniverseProxyMetrics | None = None,
) -> SectorMetrics:
    """Compute the full raw metric evidence for one primary sector (un-calibrated).

    ``expected_identities`` is the SECTOR-2 membership set for ``sector_id``. Observations
    must all belong to that sector and trading date and be unique; violations fail closed.
    Stale observations lower coverage but never contribute to metrics; missing constituents
    are never treated as unchanged.
    """
    _validate_scope(sector_id, expected_identities, observations, trading_date)
    fresh = [o for o in observations if _is_fresh(o, evaluation_time, policy.freshness_limit)]
    constituents = tuple(calculate_constituent_metrics(o, policy.direction_epsilon) for o in fresh)
    expected_count = len(expected_identities)
    valid_count = len(fresh)
    stale_count = len(observations) - valid_count
    missing_count = expected_count - len(observations)
    coverage = _ratio(valid_count, expected_count) or Decimal(0)

    breadth = _breadth(constituents)
    median_intraday = statistics.median([c.intraday_return for c in constituents])
    median_overnight = statistics.median([c.overnight_return for c in constituents])
    median_total = statistics.median([c.total_return for c in constituents])
    median_tilt = _tilt(median_intraday, policy.direction_epsilon)
    breadth_tilt = _tilt(breadth.net_breadth, Decimal(0))
    participant_count, participation_ratio = _participation(
        median_tilt, constituents, policy.direction_epsilon
    )
    proxy_value = universe_proxy.median_intraday_return if universe_proxy is not None else None
    return SectorMetrics(
        sector_id=sector_id,
        trading_date=trading_date,
        evaluation_timestamp=evaluation_time,
        expected_count=expected_count,
        valid_count=valid_count,
        stale_count=stale_count,
        missing_count=missing_count,
        invalid_count=0,
        coverage_ratio=coverage,
        breadth=breadth,
        median_overnight_return=median_overnight,
        median_intraday_return=median_intraday,
        median_total_return=median_total,
        dispersion=SectorDispersion(
            mad_intraday_return=statistics.mad([c.intraday_return for c in constituents]),
            iqr_intraday_return=statistics.iqr([c.intraday_return for c in constituents]),
        ),
        directional_agreement=_agreement(median_tilt, breadth) if valid_count else None,
        directional_participant_count=participant_count,
        directional_participation_ratio=participation_ratio,
        raw_direction=_raw_direction(median_tilt, breadth_tilt, valid_count, coverage, policy),
        universe_proxy_intraday_return=proxy_value,
        relative_strength=calculate_relative_strength(median_intraday, proxy_value),
        constituents=constituents,
    )
