"""SECTOR-4 tests: stock participation + within-sector ranking (S4-01..S4-50).

SectorMetrics context is built with the real SECTOR-3 engine; ranking is SECTOR-4. Returns
are Decimal ratios. Symmetry / order / scale invariance are asserted explicitly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.market_intelligence.sector.metrics import (
    CalculationPolicy,
    ConstituentDirection,
    ConstituentObservation,
    RawSectorDirection,
    calculate_sector_metrics,
    calculate_universe_proxy,
)
from app.market_intelligence.sector.participation import (
    DuplicateRankedConstituentError,
    StockExclusionReason,
    StockSectorAlignment,
    StockSectorContextMismatchError,
    calculate_stock_sector_metrics,
    rank_sector_constituents,
)

D = date(2026, 9, 2)
T = datetime(2026, 9, 2, 6, 0, tzinfo=UTC)
EPS = Decimal("0.001")
LIMIT = timedelta(minutes=2)
SECTOR = "AUTO"


def _policy(**kw: object) -> CalculationPolicy:
    base: dict[str, object] = {"direction_epsilon": EPS, "freshness_limit": LIMIT}
    base.update(kw)
    return CalculationPolicy(**base)  # type: ignore[arg-type]


def _obs(
    ident: str,
    intraday: Decimal,
    *,
    overnight: Decimal = Decimal(0),
    ts: datetime = T,
    td: date = D,
    sector: str = SECTOR,
) -> ConstituentObservation:
    prev = Decimal(100)
    open_ = prev * (Decimal(1) + overnight)
    return ConstituentObservation(
        identity=ident,
        sector_id=sector,
        trading_date=td,
        observation_timestamp=ts,
        last_price=open_ * (Decimal(1) + intraday),
        previous_close=prev,
        session_open=open_,
    )


def _rank(intradays: dict[str, Decimal], *, universe: list[Decimal] | None = None, **kw: object):
    obs = [_obs(i, r) for i, r in intradays.items()]
    pol = _policy(**kw)
    proxy = None
    if universe is not None:
        proxy = calculate_universe_proxy(
            [_obs(f"U{i}", r) for i, r in enumerate(universe)], pol, D, T
        )
    sm = calculate_sector_metrics(SECTOR, frozenset(intradays), obs, pol, D, T, proxy)
    return rank_sector_constituents(sm, obs, pol), sm


def _by_id(ranking) -> dict[str, object]:
    return {s.identity: s for s in (*ranking.ranked_stocks, *ranking.excluded_stocks)}


# --- relative dimensions (S4-01..S4-07, S4-41) ---


def test_s4_01_02_03_stock_vs_sector() -> None:
    r, sm = _rank({"A": Decimal("0.02"), "B": Decimal("0.012"), "C": Decimal("0.008")})
    m = _by_id(r)
    assert m["A"].stock_vs_sector == Decimal("0.02") - sm.median_intraday_return
    assert m["A"].stock_vs_sector > 0 and m["C"].stock_vs_sector < 0
    assert m["B"].stock_vs_sector == Decimal("0.012") - sm.median_intraday_return  # B == median → 0
    assert m["B"].stock_vs_sector == 0


def test_s4_04_05_41_stock_vs_universe() -> None:
    r, _ = _rank(
        {"A": Decimal("0.014"), "B": Decimal("0.012"), "C": Decimal("0.010")},
        universe=[Decimal("0.003"), Decimal("0.003"), Decimal("0.003")],
    )
    m = _by_id(r)
    assert m["A"].universe_proxy_intraday_return == Decimal("0.003")
    assert m["A"].stock_vs_universe == Decimal("0.011")
    # absolute + but universe-lagging is representable
    r2, _ = _rank(
        {"A": Decimal("0.005"), "B": Decimal("0.006"), "C": Decimal("0.007")},
        universe=[Decimal("0.010"), Decimal("0.010"), Decimal("0.010")],
    )
    assert _by_id(r2)["A"].stock_vs_universe == Decimal("-0.005")


def test_s4_06_07_absolute_positive_but_lagging() -> None:
    # sector median +1.2%, stock +0.3%, universe +0.1%
    r, sm = _rank(
        {
            "A": Decimal("0.003"),
            "B": Decimal("0.012"),
            "C": Decimal("0.012"),
            "D": Decimal("0.013"),
        },
        universe=[Decimal("0.001")],
    )
    a = _by_id(r)["A"]
    assert a.stock_intraday_return > 0  # absolute bullish
    assert a.stock_vs_sector < 0  # lagging sector
    assert a.stock_vs_universe > 0  # outperforming universe
    assert a.sector_alignment is StockSectorAlignment.ALIGNED


# --- alignment (S4-08..S4-14) ---


def test_s4_08_09_bullish_alignment() -> None:
    r, _ = _rank({"A": Decimal("0.02"), "B": Decimal("0.015"), "C": Decimal("-0.02")})
    m = _by_id(r)
    assert r.sector_raw_direction is RawSectorDirection.BULLISH
    assert m["A"].sector_alignment is StockSectorAlignment.ALIGNED
    assert m["C"].sector_alignment is StockSectorAlignment.OPPOSED


def test_s4_10_11_bearish_alignment() -> None:
    r, _ = _rank({"A": Decimal("-0.02"), "B": Decimal("-0.015"), "C": Decimal("0.02")})
    m = _by_id(r)
    assert r.sector_raw_direction is RawSectorDirection.BEARISH
    assert m["A"].sector_alignment is StockSectorAlignment.ALIGNED
    assert m["C"].sector_alignment is StockSectorAlignment.OPPOSED


def test_s4_12_neutral_sector() -> None:
    r, _ = _rank({"A": Decimal("0.0"), "B": Decimal("0.0"), "C": Decimal("0.0")})
    assert r.sector_raw_direction is RawSectorDirection.NEUTRAL
    assert r.directional_ranking_available is False
    for s in r.ranked_stocks:
        assert s.within_sector_rank is None and s.directional_strength is None
        assert s.sector_alignment is StockSectorAlignment.NEUTRAL


def test_s4_13_mixed_sector() -> None:
    # median NEUTRAL, breadth positive -> MIXED (SECTOR-3 semantics)
    r, _ = _rank(
        {
            "A": Decimal("0.0005"),
            "B": Decimal("0.0005"),
            "C": Decimal("0.0005"),
            "D": Decimal("0.02"),
            "E": Decimal("0.02"),
        }
    )
    assert r.sector_raw_direction is RawSectorDirection.MIXED
    assert r.directional_ranking_available is False
    assert all(s.sector_alignment is StockSectorAlignment.MIXED_CONTEXT for s in r.ranked_stocks)


def test_s4_14_insufficient_sector() -> None:
    r, _ = _rank({"A": Decimal("0.02"), "B": Decimal("0.02")}, minimum_valid_count=5)
    assert r.sector_raw_direction is RawSectorDirection.INSUFFICIENT_DATA
    assert r.directional_ranking_available is False
    assert all(
        s.sector_alignment is StockSectorAlignment.INSUFFICIENT_DATA for s in r.ranked_stocks
    )


# --- directional strength + ranking (S4-15..S4-24, S4-28) ---


def test_s4_15_16_17_directional_strength_symmetry() -> None:
    bull, _ = _rank({"A": Decimal("0.02"), "B": Decimal("0.012"), "C": Decimal("-0.005")})
    bear, _ = _rank({"A": Decimal("-0.02"), "B": Decimal("-0.012"), "C": Decimal("0.005")})
    mb, mr = _by_id(bull), _by_id(bear)
    assert mb["A"].directional_strength == Decimal("0.02")  # bullish: +intraday
    assert mr["A"].directional_strength == Decimal("0.02")  # bearish: -intraday, mirrors
    assert mb["A"].within_sector_rank == mr["A"].within_sector_rank == 1
    assert mb["A"].within_sector_percentile == mr["A"].within_sector_percentile


def test_s4_18_deterministic_rank() -> None:
    r, _ = _rank(
        {
            "A": Decimal("0.02"),
            "B": Decimal("0.012"),
            "C": Decimal("0.008"),
            "D": Decimal("0.002"),
            "E": Decimal("-0.004"),
        }
    )
    ranks = {s.identity: s.within_sector_rank for s in r.ranked_stocks}
    assert ranks == {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5}
    assert [s.identity for s in r.ranked_stocks] == ["A", "B", "C", "D", "E"]


def test_s4_19_20_tie_semantics_and_ordering() -> None:
    r, _ = _rank({"B": Decimal("0.02"), "A": Decimal("0.02"), "C": Decimal("0.005")})
    m = _by_id(r)
    assert m["A"].within_sector_rank == m["B"].within_sector_rank == 1  # shared analytical rank
    assert m["C"].within_sector_rank == 3  # competition ranking skips 2
    # deterministic display order: tie broken by identity, not input order
    assert [s.identity for s in r.ranked_stocks] == ["A", "B", "C"]


def test_s4_21_22_23_percentile() -> None:
    r, _ = _rank({"A": Decimal("0.02"), "B": Decimal("0.012"), "C": Decimal("0.004")})
    m = _by_id(r)
    assert m["A"].within_sector_percentile == Decimal(1)  # strongest
    assert m["C"].within_sector_percentile == Decimal(0)  # weakest
    tie, _ = _rank({"A": Decimal("0.02"), "B": Decimal("0.02"), "C": Decimal("0.004")})
    mt = _by_id(tie)
    assert mt["A"].within_sector_percentile == mt["B"].within_sector_percentile == Decimal(1)


def test_s4_24_percentile_n1_none() -> None:
    r, _ = _rank({"A": Decimal("0.02")})
    a = _by_id(r)["A"]
    assert a.within_sector_rank == 1 and a.within_sector_percentile is None  # no false superiority


def test_s4_28_outlier_ranks_strongest_without_dominating_median() -> None:
    r, sm = _rank(
        {"A": Decimal("0.10"), "B": Decimal("0.006"), "C": Decimal("0.005"), "D": Decimal("0.004")}
    )
    assert sm.median_intraday_return == Decimal("0.0055")  # robust median unaffected by +10%
    assert r.ranked_stocks[0].identity == "A" and r.ranked_stocks[0].within_sector_rank == 1


# --- robust relative magnitude (S4-25..S4-27) ---


def test_s4_25_26_27_robust_relative_magnitude() -> None:
    r, sm = _rank(
        {"A": Decimal("0.02"), "B": Decimal("0.012"), "C": Decimal("0.008"), "D": Decimal("0.004")}
    )
    a = _by_id(r)["A"]
    mad = sm.dispersion.mad_intraday_return
    assert mad is not None and mad > 0
    assert a.robust_relative_magnitude == (Decimal("0.02") - sm.median_intraday_return) / mad
    # MAD zero -> None
    r0, sm0 = _rank({"A": Decimal("0.01"), "B": Decimal("0.01"), "C": Decimal("0.01")})
    assert sm0.dispersion.mad_intraday_return == 0
    assert all(s.robust_relative_magnitude is None for s in r0.ranked_stocks)


# --- fail-closed safety (S4-29..S4-33) ---


def test_s4_29_stale_excluded() -> None:
    obs = [
        _obs("A", Decimal("0.02"), ts=T - timedelta(minutes=10)),  # strongest but stale
        _obs("B", Decimal("0.012")),
        _obs("C", Decimal("0.008")),
    ]
    pol = _policy()
    sm = calculate_sector_metrics(SECTOR, frozenset({"A", "B", "C"}), obs, pol, D, T)
    r = rank_sector_constituents(sm, obs, pol)
    ids = [s.identity for s in r.ranked_stocks]
    assert "A" not in ids and r.eligible_count == 2
    excl = {s.identity: s for s in r.excluded_stocks}
    assert excl["A"].eligible is False and excl["A"].exclusion_reason is StockExclusionReason.STALE
    assert excl["A"].within_sector_rank is None


def test_s4_30_missing_not_ranked() -> None:
    obs = [_obs("A", Decimal("0.02")), _obs("B", Decimal("0.012"))]  # C expected but absent
    pol = _policy()
    sm = calculate_sector_metrics(SECTOR, frozenset({"A", "B", "C"}), obs, pol, D, T)
    r = rank_sector_constituents(sm, obs, pol)
    assert {s.identity for s in r.ranked_stocks} == {"A", "B"}  # C simply absent


def test_s4_31_duplicate_rejected() -> None:
    obs = [_obs("A", Decimal("0.02")), _obs("A", Decimal("0.01"))]
    pol = _policy()
    sm = calculate_sector_metrics(SECTOR, frozenset({"A"}), [obs[0]], pol, D, T)
    with pytest.raises(DuplicateRankedConstituentError):
        rank_sector_constituents(sm, obs, pol)


def test_s4_32_wrong_sector_rejected() -> None:
    pol = _policy()
    sm = calculate_sector_metrics(SECTOR, frozenset({"A"}), [_obs("A", Decimal("0.02"))], pol, D, T)
    wrong = [_obs("A", Decimal("0.02"), sector="FINANCIAL_SERVICES")]
    with pytest.raises(StockSectorContextMismatchError):
        rank_sector_constituents(sm, wrong, pol)


def test_s4_33_wrong_date_rejected() -> None:
    pol = _policy()
    sm = calculate_sector_metrics(SECTOR, frozenset({"A"}), [_obs("A", Decimal("0.02"))], pol, D, T)
    wrong = [_obs("A", Decimal("0.02"), td=date(2026, 9, 3))]
    with pytest.raises(StockSectorContextMismatchError):
        rank_sector_constituents(sm, wrong, pol)


# --- invariances (S4-34, S4-35, S4-37, S4-56) ---


def test_s4_34_order_invariance() -> None:
    data = {
        "A": Decimal("0.02"),
        "B": Decimal("0.012"),
        "C": Decimal("0.008"),
        "D": Decimal("-0.004"),
    }
    r1, _ = _rank(data)
    r2, _ = _rank(dict(reversed(list(data.items()))))
    assert [
        (s.identity, s.within_sector_rank, s.within_sector_percentile) for s in r1.ranked_stocks
    ] == [(s.identity, s.within_sector_rank, s.within_sector_percentile) for s in r2.ranked_stocks]


def test_s4_35_scale_invariance() -> None:
    pol = _policy()
    a = ConstituentObservation(
        identity="A",
        sector_id=SECTOR,
        trading_date=D,
        observation_timestamp=T,
        last_price=Decimal(102),
        previous_close=Decimal(100),
        session_open=Decimal(100),
    )
    b = ConstituentObservation(
        identity="B",
        sector_id=SECTOR,
        trading_date=D,
        observation_timestamp=T,
        last_price=Decimal(1020),
        previous_close=Decimal(1000),
        session_open=Decimal(1000),
    )
    sm = calculate_sector_metrics(SECTOR, frozenset({"A", "B"}), [a, b], pol, D, T)
    r = rank_sector_constituents(sm, [a, b], pol)
    m = _by_id(r)
    assert m["A"].stock_intraday_return == m["B"].stock_intraday_return == Decimal("0.02")


def test_s4_37_bearish_mirror_ranking() -> None:
    bull, _ = _rank(
        {
            "A": Decimal("0.02"),
            "B": Decimal("0.012"),
            "C": Decimal("0.008"),
            "D": Decimal("0.002"),
            "E": Decimal("-0.004"),
        }
    )
    bear, _ = _rank(
        {
            "A": Decimal("-0.02"),
            "B": Decimal("-0.012"),
            "C": Decimal("-0.008"),
            "D": Decimal("-0.002"),
            "E": Decimal("0.004"),
        }
    )
    assert [s.identity for s in bull.ranked_stocks] == [s.identity for s in bear.ranked_stocks]


def test_s4_56_one_member_sector() -> None:
    r, _ = _rank({"A": Decimal("0.01")})
    a = _by_id(r)["A"]
    assert a.within_sector_rank == 1 and a.within_sector_percentile is None


# --- lagging / opposition raw evidence (S4-38..S4-40) ---


def test_s4_38_positive_but_sector_lagging() -> None:
    r, sm = _rank(
        {
            "A": Decimal("0.003"),
            "B": Decimal("0.012"),
            "C": Decimal("0.012"),
            "D": Decimal("0.013"),
        },
        universe=[Decimal("0.001")],
    )
    a = _by_id(r)["A"]
    assert a.stock_direction is ConstituentDirection.ADVANCING
    assert a.sector_alignment is StockSectorAlignment.ALIGNED
    assert a.stock_vs_sector < 0 and a.within_sector_rank == 4


def test_s4_39_bearish_lagging() -> None:
    r, _ = _rank(
        {
            "A": Decimal("-0.003"),
            "B": Decimal("-0.012"),
            "C": Decimal("-0.012"),
            "D": Decimal("-0.013"),
        }
    )
    a = _by_id(r)["A"]
    assert a.sector_alignment is StockSectorAlignment.ALIGNED  # bearish-aligned
    assert a.stock_vs_sector > 0  # weaker (less negative) than sector median
    assert a.directional_strength < _by_id(r)["D"].directional_strength  # lags strongest bear


def test_s4_40_opposition() -> None:
    bull, _ = _rank({"A": Decimal("0.02"), "B": Decimal("0.015"), "C": Decimal("-0.01")})
    assert _by_id(bull)["C"].sector_alignment is StockSectorAlignment.OPPOSED
    bear, _ = _rank({"A": Decimal("-0.02"), "B": Decimal("-0.015"), "C": Decimal("0.01")})
    assert _by_id(bear)["C"].sector_alignment is StockSectorAlignment.OPPOSED


# --- immutability + scope (S4-42..S4-49) ---


def test_s4_42_43_immutable_outputs() -> None:
    r, _ = _rank({"A": Decimal("0.02"), "B": Decimal("0.01")})
    with pytest.raises(ValidationError):
        r.eligible_count = 0  # type: ignore[misc]
    with pytest.raises(ValidationError):
        r.ranked_stocks[0].within_sector_rank = 5  # type: ignore[misc]


def test_s4_44_49_no_forbidden_fields() -> None:
    r, _ = _rank({"A": Decimal("0.02"), "B": Decimal("0.01")})
    fields = set(type(r.ranked_stocks[0]).model_fields)
    for forbidden in ("participation_score", "score", "confidence", "leader", "sector_score"):
        assert forbidden not in fields


def test_calculate_stock_sector_metrics_single() -> None:
    pol = _policy()
    obs = [_obs("A", Decimal("0.02")), _obs("B", Decimal("0.01")), _obs("C", Decimal("-0.005"))]
    sm = calculate_sector_metrics(SECTOR, frozenset({"A", "B", "C"}), obs, pol, D, T)
    one = calculate_stock_sector_metrics(obs[0], sm, pol)
    assert one.identity == "A" and one.eligible is True and one.within_sector_rank is None
