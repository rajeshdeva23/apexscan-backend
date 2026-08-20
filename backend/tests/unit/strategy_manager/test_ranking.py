"""Deterministic presentation ranking of strategy results (P5.5; docs/07 §14, D11)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.strategies.enums import EvaluationStatus
from app.strategy_manager.ranking import RankedStrategyResult, rank_results
from tests.unit.strategy_manager import builders as b


def test_results_are_ordered_by_descending_score() -> None:
    ranked = rank_results(
        [
            b.result(strategy_id="low", score="1"),
            b.result(strategy_id="high", score="9"),
            b.result(strategy_id="mid", score="5"),
        ]
    )
    assert [item.result.strategy_id for item in ranked] == ["high", "mid", "low"]
    assert [item.rank for item in ranked] == [1, 2, 3]


def test_ties_break_by_ascending_strategy_id() -> None:
    ranked = rank_results(
        [
            b.result(strategy_id="beta", score="5"),
            b.result(strategy_id="alpha", score="5"),
        ]
    )
    assert [item.result.strategy_id for item in ranked] == ["alpha", "beta"]


def test_a_matched_result_without_a_score_is_excluded_from_ranking() -> None:
    ranked = rank_results(
        [
            b.result(strategy_id="scored", score="3"),
            b.result(strategy_id="unscored", score=None),
        ]
    )
    assert [item.result.strategy_id for item in ranked] == ["scored"]


def test_no_match_and_error_never_receive_a_rank() -> None:
    ranked = rank_results(
        [
            b.result(strategy_id="m", status=EvaluationStatus.MATCHED, score="5"),
            b.result(
                strategy_id="n", status=EvaluationStatus.NO_MATCH, score=None, reason_codes=()
            ),
            b.result(strategy_id="e", status=EvaluationStatus.ERROR, score=None, reason_codes=()),
        ]
    )
    assert [item.result.strategy_id for item in ranked] == ["m"]


def test_empty_input_ranks_to_an_empty_projection() -> None:
    assert rank_results([]) == ()


def test_ranking_is_deterministic_and_order_independent() -> None:
    forward = rank_results(
        [b.result(strategy_id="a", score="1"), b.result(strategy_id="b", score="2")]
    )
    reverse = rank_results(
        [b.result(strategy_id="b", score="2"), b.result(strategy_id="a", score="1")]
    )
    assert forward == reverse


def test_ranking_does_not_mutate_or_re_score_the_result() -> None:
    original = b.result(strategy_id="a", score="7")
    ranked = rank_results([original])
    assert ranked[0].result == original  # carried through, never re-scored
    assert not hasattr(original, "rank")  # rank lives only on the projection


def test_ranked_projection_is_immutable() -> None:
    ranked = rank_results([b.result(score="1")])[0]
    with pytest.raises(ValidationError):
        ranked.rank = 99  # type: ignore[misc]


def test_rank_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        RankedStrategyResult(rank=0, result=b.result(score="1"))
