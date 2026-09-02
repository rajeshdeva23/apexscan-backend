"""SECTOR-3 tests: pure metrics engine (S3-01..S3-60).

Returns are Decimal ratios (0.01 == 1%). Vectors are hand-verifiable; statistics are not
cross-checked against another statistics library. Symmetry/order/scale invariance are
asserted explicitly.
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
    DuplicateConstituentError,
    MixedTradingDateError,
    RawSectorDirection,
    SectorMembershipMismatchError,
    calculate_constituent_metrics,
    calculate_relative_strength,
    calculate_sector_metrics,
    calculate_universe_proxy,
    statistics,
)

D = date(2026, 9, 2)
T = datetime(2026, 9, 2, 6, 0, tzinfo=UTC)
EPS = Decimal("0.001")  # test-only epsilon, NOT a production value
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
    ltp = open_ * (Decimal(1) + intraday)
    return ConstituentObservation(
        identity=ident,
        sector_id=sector,
        trading_date=td,
        observation_timestamp=ts,
        last_price=ltp,
        previous_close=prev,
        session_open=open_,
    )


def _sector_from(intradays: list[Decimal], **kw: object) -> object:
    obs = [_obs(f"NSE:S{i}", r) for i, r in enumerate(intradays)]
    expected = frozenset(o.identity for o in obs)
    return calculate_sector_metrics(SECTOR, expected, obs, _policy(**kw), D, T)


# --- return decomposition (S3-01..S3-05) ---


def test_s3_01_02_03_exact_returns() -> None:
    obs = ConstituentObservation(
        identity="NSE:X",
        sector_id=SECTOR,
        trading_date=D,
        observation_timestamp=T,
        last_price=Decimal(102),
        previous_close=Decimal(100),
        session_open=Decimal(105),
    )
    cm = calculate_constituent_metrics(obs, EPS)
    assert cm.overnight_return == Decimal("0.05")
    assert cm.intraday_return == Decimal(-3) / Decimal(105)
    assert cm.total_return == Decimal("0.02")


def test_s3_04_gap_up_selloff_is_intraday_bearish() -> None:
    cm = calculate_constituent_metrics(
        _obs("NSE:X", Decimal("-0.02"), overnight=Decimal("0.05")), EPS
    )
    assert cm.overnight_return > 0 and cm.intraday_return < 0
    assert cm.direction is ConstituentDirection.DECLINING


def test_s3_05_gap_down_recovery_is_intraday_bullish() -> None:
    cm = calculate_constituent_metrics(
        _obs("NSE:X", Decimal("0.02"), overnight=Decimal("-0.05")), EPS
    )
    assert cm.overnight_return < 0 and cm.intraday_return > 0
    assert cm.direction is ConstituentDirection.ADVANCING


# --- direction + breadth (S3-06..S3-13) ---


def test_s3_06_07_08_direction_thresholds() -> None:
    assert (
        calculate_constituent_metrics(_obs("A", Decimal("0.005")), EPS).direction
        is ConstituentDirection.ADVANCING
    )
    assert (
        calculate_constituent_metrics(_obs("B", Decimal("-0.005")), EPS).direction
        is ConstituentDirection.DECLINING
    )
    assert (
        calculate_constituent_metrics(_obs("C", Decimal("0.0005")), EPS).direction
        is ConstituentDirection.UNCHANGED
    )


def test_s3_09_10_11_breadth() -> None:
    up = _sector_from([Decimal("0.01"), Decimal("0.02"), Decimal("0.03")])
    assert up.breadth.advance_count == 3 and up.breadth.net_breadth == Decimal(1)
    down = _sector_from([Decimal("-0.01"), Decimal("-0.02"), Decimal("-0.03")])
    assert down.breadth.decline_count == 3 and down.breadth.net_breadth == Decimal(-1)
    bal = _sector_from([Decimal("0.01"), Decimal("-0.01")])
    assert bal.breadth.net_breadth == Decimal(0)


def test_s3_12_13_ratios_sum_and_bounds() -> None:
    m = _sector_from([Decimal("0.01"), Decimal("-0.01"), Decimal("0.0")])
    b = m.breadth
    assert b.advance_count + b.decline_count + b.unchanged_count == b.valid_count  # exact
    ratio_sum = b.advance_ratio + b.decline_ratio + b.unchanged_ratio
    assert abs(ratio_sum - Decimal(1)) < Decimal("1e-25")  # exact up to Decimal precision
    assert Decimal(-1) <= b.net_breadth <= Decimal(1)


# --- central tendency + dispersion (S3-14..S3-20) ---


def test_s3_14_15_median_odd_even() -> None:
    assert statistics.median([Decimal(1), Decimal(3), Decimal(2)]) == Decimal(2)
    assert statistics.median([Decimal(1), Decimal(2), Decimal(3), Decimal(4)]) == Decimal("2.5")


def test_s3_16_mad_known_vector() -> None:
    # values 1,2,4,6,9 -> median 4 -> deviations 3,2,0,2,5 -> median 2
    assert statistics.mad([Decimal(1), Decimal(2), Decimal(4), Decimal(6), Decimal(9)]) == Decimal(
        2
    )


def test_s3_17_iqr_known_vector() -> None:
    # 1..7 odd: median 4 dropped; lower [1,2,3] Q1=2; upper [5,6,7] Q3=6; IQR=4
    vals = [Decimal(i) for i in (1, 2, 3, 4, 5, 6, 7)]
    assert statistics.iqr(vals) == Decimal(4)
    # even N=4: [1,2,3,4] lower [1,2] Q1=1.5 upper [3,4] Q3=3.5 IQR=2
    assert statistics.iqr([Decimal(1), Decimal(2), Decimal(3), Decimal(4)]) == Decimal(2)


def test_s3_18_small_n_dispersion() -> None:
    assert statistics.mad([Decimal(5)]) == Decimal(0)
    assert statistics.iqr([Decimal(5)]) is None
    assert statistics.iqr([]) is None
    assert statistics.mad([]) is None
    assert statistics.iqr([Decimal(1), Decimal(4)]) == Decimal(3)


def test_s3_19_outlier_resistance() -> None:
    bull = _sector_from(
        [Decimal("0.004"), Decimal("0.005"), Decimal("0.006"), Decimal("0.005"), Decimal("0.08")]
    )
    assert bull.median_intraday_return == Decimal("0.005")
    bear = _sector_from(
        [
            Decimal("-0.004"),
            Decimal("-0.005"),
            Decimal("-0.006"),
            Decimal("-0.005"),
            Decimal("-0.08"),
        ]
    )
    assert bear.median_intraday_return == Decimal("-0.005")


def test_s3_20_heavyweight_distortion_not_bullish() -> None:
    vals = [
        Decimal("-0.002"),
        Decimal("-0.001"),
        Decimal(0),
        Decimal(0),
        Decimal("-0.001"),
        Decimal("-0.002"),
        Decimal(0),
        Decimal("-0.001"),
        Decimal(0),
        Decimal("0.05"),
    ]
    m = _sector_from(vals)
    assert m.breadth.advance_count == 1
    assert m.median_intraday_return <= 0
    assert m.breadth.net_breadth <= 0
    assert m.raw_direction is not RawSectorDirection.BULLISH


# --- agreement / participation / raw direction (S3-21..S3-29) ---


def test_s3_21_22_23_agreement() -> None:
    bull = _sector_from([Decimal("0.01"), Decimal("0.02"), Decimal("-0.01")])
    assert bull.directional_agreement == Decimal(2) / Decimal(3)
    bear = _sector_from([Decimal("-0.01"), Decimal("-0.02"), Decimal("0.01")])
    assert bear.directional_agreement == Decimal(2) / Decimal(3)
    neutral = _sector_from([Decimal("0.0"), Decimal("0.0"), Decimal("0.01")])
    assert neutral.median_intraday_return == Decimal(0)
    assert neutral.directional_agreement == neutral.breadth.unchanged_ratio


def test_s3_24_25_participation() -> None:
    bull = _sector_from([Decimal("0.01"), Decimal("0.02"), Decimal("0.03"), Decimal("-0.01")])
    assert bull.directional_participant_count == 3
    assert bull.directional_participation_ratio == Decimal(3) / Decimal(4)
    bear = _sector_from([Decimal("-0.01"), Decimal("-0.02"), Decimal("-0.03"), Decimal("0.01")])
    assert bear.directional_participant_count == 3


def test_s3_26_27_28_29_raw_direction() -> None:
    assert (
        _sector_from([Decimal("0.01"), Decimal("0.02"), Decimal("0.03")]).raw_direction
        is RawSectorDirection.BULLISH
    )
    assert (
        _sector_from([Decimal("-0.01"), Decimal("-0.02"), Decimal("-0.03")]).raw_direction
        is RawSectorDirection.BEARISH
    )
    assert (
        _sector_from([Decimal("0.0"), Decimal("0.0"), Decimal("0.0")]).raw_direction
        is RawSectorDirection.NEUTRAL
    )
    # median NEUTRAL (within epsilon) but breadth positive -> disagreement -> MIXED
    mixed = _sector_from(
        [Decimal("0.0005"), Decimal("0.0005"), Decimal("0.0005"), Decimal("0.02"), Decimal("0.02")]
    )
    assert mixed.median_intraday_return == Decimal("0.0005")  # <= epsilon -> neutral tilt
    assert mixed.breadth.net_breadth > 0  # two advancing, none declining
    assert mixed.raw_direction is RawSectorDirection.MIXED


# --- universe proxy + relative strength (S3-30..S3-35) ---


def test_s3_30_31_32_universe_proxy() -> None:
    obs = [
        _obs(f"NSE:U{i}", r, sector="MIX")
        for i, r in enumerate([Decimal("0.01"), Decimal("0.02"), Decimal("0.03")])
    ]
    assert calculate_universe_proxy(obs, _policy(), D, T).median_intraday_return == Decimal("0.02")
    obs4 = [
        _obs(f"NSE:U{i}", r)
        for i, r in enumerate([Decimal("0.01"), Decimal("0.02"), Decimal("0.03"), Decimal("0.05")])
    ]
    assert calculate_universe_proxy(obs4, _policy(), D, T).median_intraday_return == Decimal(
        "0.025"
    )
    outlier = [
        _obs(f"NSE:U{i}", r)
        for i, r in enumerate([Decimal("0.004"), Decimal("0.005"), Decimal("0.9")])
    ]
    assert calculate_universe_proxy(outlier, _policy(), D, T).median_intraday_return == Decimal(
        "0.005"
    )


def test_s3_33_34_35_relative_strength() -> None:
    assert calculate_relative_strength(Decimal("0.012"), Decimal("0.004")) == Decimal("0.008")
    assert calculate_relative_strength(Decimal("-0.005"), Decimal("0.002")) == Decimal("-0.007")
    # positive absolute, negative relative
    assert calculate_relative_strength(Decimal("0.002"), Decimal("0.010")) == Decimal("-0.008")
    assert calculate_relative_strength(None, Decimal("0.01")) is None


# --- coverage / freshness / missing / stale (S3-36..S3-41) ---


def _sector_with_expected(
    obs: list[ConstituentObservation], expected: frozenset[str], **kw: object
) -> object:
    return calculate_sector_metrics(SECTOR, expected, obs, _policy(**kw), D, T)


def test_s3_36_full_coverage() -> None:
    obs = [_obs(f"NSE:S{i}", Decimal("0.01")) for i in range(10)]
    m = _sector_with_expected(obs, frozenset(o.identity for o in obs))
    assert m.expected_count == 10 and m.valid_count == 10 and m.coverage_ratio == Decimal(1)


def test_s3_37_missing_lowers_coverage() -> None:
    obs = [_obs(f"NSE:S{i}", Decimal("0.01")) for i in range(8)]
    expected = frozenset(f"NSE:S{i}" for i in range(10))
    m = _sector_with_expected(obs, expected)
    assert m.valid_count == 8 and m.missing_count == 2 and m.coverage_ratio == Decimal("0.8")


def test_s3_38_39_40_stale_excluded_missing_not_unchanged() -> None:
    fresh = [_obs(f"NSE:S{i}", Decimal("0.01")) for i in range(8)]
    stale = [_obs(f"NSE:S{i}", Decimal("0.01"), ts=T - timedelta(minutes=10)) for i in (8, 9)]
    expected = frozenset(f"NSE:S{i}" for i in range(10))
    m = _sector_with_expected(fresh + stale, expected)
    assert m.valid_count == 8 and m.stale_count == 2 and m.missing_count == 0
    assert m.coverage_ratio == Decimal("0.8")
    assert m.breadth.valid_count == 8  # stale never counted as unchanged/advancing


def test_s3_41_zero_valid_insufficient() -> None:
    expected = frozenset(f"NSE:S{i}" for i in range(5))
    m = _sector_with_expected([], expected)
    assert m.valid_count == 0 and m.coverage_ratio == Decimal(0)
    assert m.raw_direction is RawSectorDirection.INSUFFICIENT_DATA
    assert m.median_intraday_return is None and m.directional_participation_ratio is None


# --- safety / fail-closed (S3-42..S3-47) ---


def test_s3_42_duplicate_rejected() -> None:
    obs = [_obs("NSE:S0", Decimal("0.01")), _obs("NSE:S0", Decimal("0.02"))]
    with pytest.raises(DuplicateConstituentError):
        calculate_sector_metrics(SECTOR, frozenset({"NSE:S0"}), obs, _policy(), D, T)


def test_s3_43_mixed_trading_date_rejected() -> None:
    obs = [_obs("NSE:S0", Decimal("0.01")), _obs("NSE:S1", Decimal("0.01"), td=date(2026, 9, 3))]
    with pytest.raises(MixedTradingDateError):
        calculate_sector_metrics(SECTOR, frozenset({"NSE:S0", "NSE:S1"}), obs, _policy(), D, T)


def test_s3_44_membership_mismatch_rejected() -> None:
    wrong_sector = [_obs("NSE:S0", Decimal("0.01"), sector="FINANCIAL_SERVICES")]
    with pytest.raises(SectorMembershipMismatchError):
        calculate_sector_metrics(SECTOR, frozenset({"NSE:S0"}), wrong_sector, _policy(), D, T)
    not_expected = [_obs("NSE:S9", Decimal("0.01"))]
    with pytest.raises(SectorMembershipMismatchError):
        calculate_sector_metrics(SECTOR, frozenset({"NSE:S0"}), not_expected, _policy(), D, T)


def test_s3_45_46_47_invalid_prices_rejected() -> None:
    for bad in ("previous_close", "session_open", "last_price"):
        kwargs = dict(
            identity="NSE:X",
            sector_id=SECTOR,
            trading_date=D,
            observation_timestamp=T,
            last_price=Decimal(100),
            previous_close=Decimal(100),
            session_open=Decimal(100),
        )
        kwargs[bad] = Decimal(0)
        with pytest.raises(ValidationError):
            ConstituentObservation(**kwargs)  # type: ignore[arg-type]


def test_s3_48_freshness_boundary() -> None:
    policy = _policy()
    at = _obs("NSE:S0", Decimal("0.01"), ts=T - LIMIT)  # age == limit -> fresh
    beyond = _obs("NSE:S1", Decimal("0.01"), ts=T - LIMIT - timedelta(seconds=1))  # stale
    m = calculate_sector_metrics(
        SECTOR, frozenset({"NSE:S0", "NSE:S1"}), [at, beyond], policy, D, T
    )
    assert m.valid_count == 1 and m.stale_count == 1


# --- invariances (S3-49..S3-56, S3-59) ---


def test_s3_49_50_51_52_bull_bear_symmetry() -> None:
    vec = [Decimal("0.012"), Decimal("0.004"), Decimal("-0.006"), Decimal("0.02"), Decimal("-0.01")]
    bull = _sector_from(vec)
    bear = _sector_from([-x for x in vec])
    assert bear.breadth.net_breadth == -bull.breadth.net_breadth
    assert bear.median_intraday_return == -bull.median_intraday_return
    assert bear.dispersion.mad_intraday_return == bull.dispersion.mad_intraday_return
    assert bear.dispersion.iqr_intraday_return == bull.dispersion.iqr_intraday_return
    assert bear.directional_participation_ratio == bull.directional_participation_ratio


def test_s3_53_order_invariance() -> None:
    vec = [Decimal("0.01"), Decimal("-0.02"), Decimal("0.03"), Decimal("0.005"), Decimal("-0.001")]
    a = _sector_from(vec)
    b = _sector_from(list(reversed(vec)))
    assert (a.median_intraday_return, a.breadth.net_breadth, a.dispersion.iqr_intraday_return) == (
        b.median_intraday_return,
        b.breadth.net_breadth,
        b.dispersion.iqr_intraday_return,
    )
    assert a.raw_direction is b.raw_direction


def test_s3_54_scale_invariance() -> None:
    a = ConstituentObservation(
        identity="A",
        sector_id=SECTOR,
        trading_date=D,
        observation_timestamp=T,
        last_price=Decimal(101),
        previous_close=Decimal(100),
        session_open=Decimal(100),
    )
    b = ConstituentObservation(
        identity="B",
        sector_id=SECTOR,
        trading_date=D,
        observation_timestamp=T,
        last_price=Decimal(1010),
        previous_close=Decimal(1000),
        session_open=Decimal(1000),
    )
    assert (
        calculate_constituent_metrics(a, EPS).intraday_return
        == calculate_constituent_metrics(b, EPS).intraday_return
    )


def test_s3_55_one_member_sector() -> None:
    m = _sector_from([Decimal("0.01")])
    assert m.valid_count == 1 and m.breadth.net_breadth == Decimal(1)
    assert m.median_intraday_return == Decimal("0.01")
    assert m.dispersion.mad_intraday_return == Decimal(0)
    assert m.dispersion.iqr_intraday_return is None  # no statistical spread from one point


def test_s3_56_no_market_cap_weighting() -> None:
    # a huge-price name and a tiny-price name with equal returns contribute equally
    obs = [
        ConstituentObservation(
            identity="BIG",
            sector_id=SECTOR,
            trading_date=D,
            observation_timestamp=T,
            last_price=Decimal(5050),
            previous_close=Decimal(5000),
            session_open=Decimal(5000),
        ),
        ConstituentObservation(
            identity="SMALL",
            sector_id=SECTOR,
            trading_date=D,
            observation_timestamp=T,
            last_price=Decimal("10.10"),
            previous_close=Decimal(10),
            session_open=Decimal(10),
        ),
    ]
    m = calculate_sector_metrics(SECTOR, frozenset({"BIG", "SMALL"}), obs, _policy(), D, T)
    assert m.median_intraday_return == Decimal("0.01") and m.breadth.net_breadth == Decimal(1)


def test_s3_59_deterministic_repeat() -> None:
    vec = [Decimal("0.01"), Decimal("-0.02"), Decimal("0.03")]
    assert _sector_from(vec).model_dump() == _sector_from(vec).model_dump()


def test_s3_58_immutable_output() -> None:
    m = _sector_from([Decimal("0.01")])
    with pytest.raises(ValidationError):
        m.valid_count = 5  # type: ignore[misc]


def test_gap_reversal_sector_direction() -> None:  # S3-50 (gap-reversal at sector level)
    gap_up_selloff = _sector_from([Decimal("-0.01"), Decimal("-0.02"), Decimal("-0.015")])
    assert gap_up_selloff.raw_direction is RawSectorDirection.BEARISH
    gap_down_recovery = _sector_from([Decimal("0.01"), Decimal("0.02"), Decimal("0.015")])
    assert gap_down_recovery.raw_direction is RawSectorDirection.BULLISH


def test_insufficient_via_policy_minimums() -> None:
    m = _sector_from([Decimal("0.01"), Decimal("0.02")], minimum_valid_count=5)
    assert m.raw_direction is RawSectorDirection.INSUFFICIENT_DATA
    cov = calculate_sector_metrics(
        SECTOR,
        frozenset(f"NSE:S{i}" for i in range(10)),
        [_obs(f"NSE:S{i}", Decimal("0.01")) for i in range(3)],
        _policy(minimum_coverage_ratio=Decimal("0.5")),
        D,
        T,
    )
    assert cov.raw_direction is RawSectorDirection.INSUFFICIENT_DATA
