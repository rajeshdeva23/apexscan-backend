"""SECTOR-VALIDATION-1: whole-universe shadow-validation over SECTOR-2/3/4.

Primitive math (median/MAD/IQR/breadth/symmetry/scale/order, ranking/percentile/alignment)
is already covered by test_sector_metrics.py and test_stock_participation.py — not duplicated.
These tests validate the orchestration, real-dataset membership integrity, reconciliation,
determinism, and fail-closed behavior.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.market_intelligence.sector import MembershipResolver, load_sector_membership_dataset
from app.market_intelligence.sector.metrics import (
    CalculationPolicy,
    DuplicateConstituentError,
    MixedTradingDateError,
    RawSectorDirection,
)
from app.tools.sector_validation import (
    ValidationObservation,
    evaluate_universe,
    to_artifact,
)

D = date(2026, 9, 2)
T = datetime(2026, 9, 2, 6, 0, tzinfo=UTC)
POLICY = CalculationPolicy(direction_epsilon=Decimal("0.001"), freshness_limit=timedelta(minutes=5))


def _resolver() -> MembershipResolver:
    return MembershipResolver(load_sector_membership_dataset())


def _obs(identity: str, intraday: Decimal, *, ts: datetime = T) -> ValidationObservation:
    prev = Decimal(100)
    return ValidationObservation(
        identity=identity,
        trading_date=D,
        observation_timestamp=ts,
        last_price=prev * (Decimal(1) + intraday),
        previous_close=prev,
        session_open=prev,
    )


def _all_identities(r: MembershipResolver) -> list[str]:
    return [i for s in r.all_primary_sectors() for i in r.members_of_primary_sector(s)]


def test_membership_integrity_full_universe() -> None:
    r = _resolver()
    ids = _all_identities(r)
    assert len(ids) == 210 and len(set(ids)) == 210  # no identity in two primary sectors
    obs = [_obs(i, Decimal("0.01")) for i in ids]
    ev = evaluate_universe(obs, r, POLICY, D, T)
    assert ev.observed_count == 210 and ev.mapped_count == 210 and ev.unmapped_identities == ()
    assert len(ev.sectors) == 18
    assert sum(s.metrics.expected_count for s in ev.sectors) == 210  # reconciles to universe
    assert ev.valid_count == 210  # all fresh+valid
    assert sum(len(s.ranking.ranked_stocks) for s in ev.sectors) == 210


def test_unmapped_identity_excluded_not_guessed() -> None:
    r = _resolver()
    obs = [_obs("NSE:RELIANCE", Decimal("0.01")), _obs("NSE:NOT_A_REAL_TICKER", Decimal("0.02"))]
    ev = evaluate_universe(obs, r, POLICY, D, T)
    assert "NSE:NOT_A_REAL_TICKER" in ev.unmapped_identities
    assert ev.mapped_count == 1


def test_coverage_partial_and_missing_not_zero() -> None:
    r = _resolver()
    members = r.members_of_primary_sector("FINANCIAL_SERVICES")
    half = members[: len(members) // 2]
    ev = evaluate_universe([_obs(i, Decimal("0.01")) for i in half], r, POLICY, D, T)
    fin = next(s for s in ev.sectors if s.sector_id == "FINANCIAL_SERVICES")
    assert fin.metrics.valid_count == len(half)
    assert fin.metrics.expected_count == len(members)
    assert 0 < fin.metrics.coverage_ratio < 1  # missing lowers coverage, not counted as 0-return


def test_heavyweight_does_not_make_sector_bullish() -> None:
    r = _resolver()
    members = list(r.members_of_primary_sector("FINANCIAL_SERVICES"))
    obs = [_obs(members[0], Decimal("0.10"))] + [_obs(m, Decimal("-0.002")) for m in members[1:]]
    ev = evaluate_universe(obs, r, POLICY, D, T)
    fin = next(s for s in ev.sectors if s.sector_id == "FINANCIAL_SERVICES")
    assert fin.metrics.raw_direction is not RawSectorDirection.BULLISH
    assert fin.metrics.median_intraday_return <= 0


def test_n1_sector_no_confidence_claim() -> None:
    r = _resolver()
    members = r.members_of_primary_sector("TEXTILES")
    assert len(members) == 1
    ev = evaluate_universe([_obs(members[0], Decimal("0.01"))], r, POLICY, D, T)
    tex = next(s for s in ev.sectors if s.sector_id == "TEXTILES")
    assert tex.metrics.valid_count == 1
    stock = tex.ranking.ranked_stocks[0]
    assert stock.within_sector_rank == 1 and stock.within_sector_percentile is None
    # no confidence/score fields exist anywhere in the artifact
    art = to_artifact(ev)
    assert all(k not in str(art) for k in ("confidence", "sector_score", "SectorScore"))


def test_determinism_and_order_invariance() -> None:
    r = _resolver()
    obs = [
        _obs(i, Decimal("0.01") if n % 2 else Decimal("-0.005"))
        for n, i in enumerate(_all_identities(r))
    ]
    a1 = to_artifact(evaluate_universe(obs, r, POLICY, D, T))
    a2 = to_artifact(evaluate_universe(list(reversed(obs)), r, POLICY, D, T))
    assert a1 == a2  # order-invariant and deterministic


def test_stale_excluded_from_universe() -> None:
    r = _resolver()
    members = r.members_of_primary_sector(
        "IT" if "IT" in r.all_primary_sectors() else "INFORMATION_TECHNOLOGY"
    )
    fresh = [_obs(m, Decimal("0.01")) for m in members[:-1]]
    stale = [_obs(members[-1], Decimal("0.05"), ts=T - timedelta(minutes=30))]
    ev = evaluate_universe(fresh + stale, r, POLICY, D, T)
    it = next(s for s in ev.sectors if s.sector_id == "INFORMATION_TECHNOLOGY")
    assert it.metrics.valid_count == len(members) - 1 and it.metrics.stale_count == 1


def test_fail_closed_duplicate_and_mixed_date() -> None:
    r = _resolver()
    with pytest.raises(DuplicateConstituentError):
        evaluate_universe(
            [_obs("NSE:RELIANCE", Decimal("0.01")), _obs("NSE:RELIANCE", Decimal("0.02"))],
            r,
            POLICY,
            D,
            T,
        )
    bad = ValidationObservation(
        identity="NSE:RELIANCE",
        trading_date=date(2026, 9, 3),
        observation_timestamp=T,
        last_price=Decimal(101),
        previous_close=Decimal(100),
        session_open=Decimal(100),
    )
    with pytest.raises(MixedTradingDateError):
        evaluate_universe([bad], r, POLICY, D, T)


def test_relative_strength_uses_universe_proxy() -> None:
    r = _resolver()
    ids = _all_identities(r)
    # everyone +0.5% except FINANCIAL_SERVICES members +2% -> FS relative strength positive
    fin = set(r.members_of_primary_sector("FINANCIAL_SERVICES"))
    obs = [_obs(i, Decimal("0.02") if i in fin else Decimal("0.005")) for i in ids]
    ev = evaluate_universe(obs, r, POLICY, D, T)
    fs = next(s for s in ev.sectors if s.sector_id == "FINANCIAL_SERVICES")
    assert ev.universe_proxy_intraday_return is not None
    assert fs.metrics.relative_strength is not None and fs.metrics.relative_strength > 0
