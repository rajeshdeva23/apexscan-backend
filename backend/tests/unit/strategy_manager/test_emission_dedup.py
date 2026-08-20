"""Emission-policy dedup semantics (P5.5; ADR-007 D10).

Covers CONTINUOUS material-change vs unchanged/context-version-only suppression,
EDGE_TRIGGERED transitions, ONE_SHOT_PER_SESSION first-match/session with new-date
reset, per-(strategy, instrument) isolation, STOP-style reset, and bounded state.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.strategies.enums import EmissionPolicy, EvaluationStatus
from app.strategies.results import MetricEntry
from app.strategy_manager.dedup import EmissionDeduplicator
from tests.unit.strategy_manager import builders as b

_DATE = date(2026, 8, 9)
_NEXT_DATE = date(2026, 8, 10)
_CONTINUOUS = EmissionPolicy.CONTINUOUS
_EDGE = EmissionPolicy.EDGE_TRIGGERED
_ONE_SHOT = EmissionPolicy.ONE_SHOT_PER_SESSION


def _emit(dedup: EmissionDeduplicator, result, policy, *, trading_date=_DATE) -> bool:
    return dedup.should_emit(result, policy=policy, trading_date=trading_date)


# --------------------------------------------------------------------------- #
# CONTINUOUS
# --------------------------------------------------------------------------- #
def test_continuous_first_observation_is_material() -> None:
    dedup = EmissionDeduplicator()
    assert _emit(dedup, b.result(score="1"), _CONTINUOUS) is True


def test_continuous_suppresses_an_unchanged_repeat() -> None:
    dedup = EmissionDeduplicator()
    assert _emit(dedup, b.result(context_version=1, score="1"), _CONTINUOUS) is True
    assert _emit(dedup, b.result(context_version=2, score="1"), _CONTINUOUS) is False


def test_continuous_context_version_alone_does_not_force_emission() -> None:
    dedup = EmissionDeduplicator()
    _emit(dedup, b.result(context_version=1, score="1", reason_codes=("A",)), _CONTINUOUS)
    # Only context_version changed — status/score/confidence/reasons/metrics identical.
    assert (
        _emit(dedup, b.result(context_version=99, score="1", reason_codes=("A",)), _CONTINUOUS)
        is False
    )


def test_continuous_emits_on_a_score_change() -> None:
    dedup = EmissionDeduplicator()
    _emit(dedup, b.result(score="1"), _CONTINUOUS)
    assert _emit(dedup, b.result(score="2"), _CONTINUOUS) is True


def test_continuous_emits_on_a_reason_code_change() -> None:
    dedup = EmissionDeduplicator()
    _emit(dedup, b.result(score="1", reason_codes=("A",)), _CONTINUOUS)
    assert _emit(dedup, b.result(score="1", reason_codes=("A", "B")), _CONTINUOUS) is True


def test_continuous_emits_on_a_metric_change() -> None:
    dedup = EmissionDeduplicator()
    _emit(
        dedup,
        b.result(score="1", metrics=(MetricEntry(name="m", value=Decimal("1")),)),
        _CONTINUOUS,
    )
    assert (
        _emit(
            dedup,
            b.result(score="1", metrics=(MetricEntry(name="m", value=Decimal("2")),)),
            _CONTINUOUS,
        )
        is True
    )


def test_continuous_emits_on_match_to_no_match_transition() -> None:
    dedup = EmissionDeduplicator()
    _emit(dedup, b.result(status=EvaluationStatus.MATCHED, score="1"), _CONTINUOUS)
    no_match = b.result(status=EvaluationStatus.NO_MATCH, score=None, reason_codes=())
    assert _emit(dedup, no_match, _CONTINUOUS) is True


# --------------------------------------------------------------------------- #
# EDGE_TRIGGERED
# --------------------------------------------------------------------------- #
def test_edge_suppresses_the_baseline_no_match() -> None:
    dedup = EmissionDeduplicator()
    no_match = b.result(status=EvaluationStatus.NO_MATCH, score=None, reason_codes=())
    assert _emit(dedup, no_match, _EDGE) is False  # NO_MATCH is the implicit baseline


def test_edge_emits_the_rising_edge_to_match() -> None:
    dedup = EmissionDeduplicator()
    no_match = b.result(status=EvaluationStatus.NO_MATCH, score=None, reason_codes=())
    _emit(dedup, no_match, _EDGE)
    assert _emit(dedup, b.result(status=EvaluationStatus.MATCHED, score="1"), _EDGE) is True


def test_edge_suppresses_a_repeated_match_even_if_score_moves() -> None:
    dedup = EmissionDeduplicator()
    assert _emit(dedup, b.result(status=EvaluationStatus.MATCHED, score="1"), _EDGE) is True
    assert _emit(dedup, b.result(status=EvaluationStatus.MATCHED, score="9"), _EDGE) is False


def test_edge_emits_the_falling_edge_back_to_no_match() -> None:
    dedup = EmissionDeduplicator()
    _emit(dedup, b.result(status=EvaluationStatus.MATCHED, score="1"), _EDGE)
    no_match = b.result(status=EvaluationStatus.NO_MATCH, score=None, reason_codes=())
    assert _emit(dedup, no_match, _EDGE) is True


# --------------------------------------------------------------------------- #
# ONE_SHOT_PER_SESSION
# --------------------------------------------------------------------------- #
def test_one_shot_emits_only_the_first_match_of_a_session() -> None:
    dedup = EmissionDeduplicator()
    assert _emit(dedup, b.result(status=EvaluationStatus.MATCHED, score="1"), _ONE_SHOT) is True
    assert _emit(dedup, b.result(status=EvaluationStatus.MATCHED, score="1"), _ONE_SHOT) is False


def test_one_shot_never_emits_a_no_match() -> None:
    dedup = EmissionDeduplicator()
    no_match = b.result(status=EvaluationStatus.NO_MATCH, score=None, reason_codes=())
    assert _emit(dedup, no_match, _ONE_SHOT) is False


def test_one_shot_resets_on_a_new_trading_date() -> None:
    dedup = EmissionDeduplicator()
    match = b.result(status=EvaluationStatus.MATCHED, score="1")
    assert _emit(dedup, match, _ONE_SHOT, trading_date=_DATE) is True
    assert _emit(dedup, match, _ONE_SHOT, trading_date=_DATE) is False
    assert _emit(dedup, match, _ONE_SHOT, trading_date=_NEXT_DATE) is True  # new session


# --------------------------------------------------------------------------- #
# Isolation, reset, and bounded state
# --------------------------------------------------------------------------- #
def test_dedup_is_isolated_per_strategy_and_instrument() -> None:
    dedup = EmissionDeduplicator()
    _emit(dedup, b.result(strategy_id="a", instrument=b.INSTRUMENT, score="1"), _CONTINUOUS)
    # A different strategy and a different instrument each start fresh.
    assert (
        _emit(dedup, b.result(strategy_id="b", instrument=b.INSTRUMENT, score="1"), _CONTINUOUS)
        is True
    )
    assert (
        _emit(
            dedup, b.result(strategy_id="a", instrument=b.OTHER_INSTRUMENT, score="1"), _CONTINUOUS
        )
        is True
    )


def test_reset_strategy_clears_only_that_strategys_state() -> None:
    dedup = EmissionDeduplicator()
    _emit(dedup, b.result(strategy_id="a", score="1"), _CONTINUOUS)
    _emit(dedup, b.result(strategy_id="b", score="1"), _CONTINUOUS)
    dedup.reset_strategy("a")
    # 'a' re-emits the same content (state gone); 'b' is untouched and still suppresses.
    assert _emit(dedup, b.result(strategy_id="a", score="1"), _CONTINUOUS) is True
    assert _emit(dedup, b.result(strategy_id="b", score="1"), _CONTINUOUS) is False


def test_state_is_bounded_by_strategy_and_instrument() -> None:
    dedup = EmissionDeduplicator()
    for version in range(1, 6):
        _emit(
            dedup,
            b.result(strategy_id="a", score=str(version), context_version=version),
            _CONTINUOUS,
        )
    assert dedup.size() == 1  # many cycles, one (strategy, instrument) entry — bounded


def test_new_trading_date_does_not_grow_state() -> None:
    dedup = EmissionDeduplicator()
    match = b.result(status=EvaluationStatus.MATCHED, score="1")
    _emit(dedup, match, _ONE_SHOT, trading_date=_DATE)
    _emit(dedup, match, _ONE_SHOT, trading_date=_NEXT_DATE)
    assert dedup.size() == 1  # the entry is replaced in place, not accumulated
