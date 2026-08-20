"""Promotion of an internal evaluation into an external StrategyResult (P5.5)."""

from __future__ import annotations

from decimal import Decimal

from app.strategies.descriptor import StrategyDescriptor
from app.strategies.enums import EmissionPolicy, EvaluationStatus, StrategyCategory
from app.strategies.results import MetricEntry, StrategyEvaluation
from app.strategy_manager.promotion import promote_evaluation
from tests.unit.strategy_manager import builders as b


def _descriptor(strategy_id: str = "alpha") -> StrategyDescriptor:
    return StrategyDescriptor(
        strategy_id=strategy_id,
        display_name="Alpha",
        description="A test strategy.",
        version="2.3.4",
        category=StrategyCategory.BREAKOUT,
        emission_policy=EmissionPolicy.CONTINUOUS,
    )


def _matched() -> StrategyEvaluation:
    return StrategyEvaluation(
        instrument=b.INSTRUMENT,
        context_version=7,
        status=EvaluationStatus.MATCHED,
        score=Decimal("42.5"),
        confidence=Decimal("0.8"),
        reason_codes=("BREAKOUT_UP",),
        metrics=(MetricEntry(name="range_pct", value=Decimal("1.2")),),
        diagnostics=(("note", "diag"),),
    )


def test_promotion_preserves_every_typed_field() -> None:
    evaluation = _matched()
    result = promote_evaluation(
        evaluation=evaluation,
        descriptor=_descriptor(),
        config_version="9.9.9",
        evaluation_timestamp=b.EVENT_TIME,
    )
    assert result.instrument == evaluation.instrument
    assert result.context_version == 7
    assert result.status is EvaluationStatus.MATCHED
    assert result.score == Decimal("42.5")
    assert result.confidence == Decimal("0.8")
    assert result.reason_codes == ("BREAKOUT_UP",)
    assert result.metrics == evaluation.metrics
    assert result.diagnostics == (("note", "diag"),)


def test_promotion_stamps_identity_versions_and_timestamp() -> None:
    result = promote_evaluation(
        evaluation=_matched(),
        descriptor=_descriptor("alpha"),
        config_version="9.9.9",
        evaluation_timestamp=b.EVENT_TIME,
    )
    assert result.strategy_id == "alpha"
    assert result.strategy_version == "2.3.4"  # from the descriptor, not the evaluation
    assert result.config_version == "9.9.9"
    assert result.evaluation_timestamp == b.EVENT_TIME  # deterministic, manager-supplied
    assert ("category", "breakout") in result.metadata


def test_promotion_never_invents_a_result_id() -> None:
    # Durable identity belongs to persistence (docs/02 §6.4); no uuid/random here.
    result = promote_evaluation(
        evaluation=_matched(),
        descriptor=_descriptor(),
        config_version="1.0.0",
        evaluation_timestamp=b.EVENT_TIME,
    )
    assert result.result_id is None


def test_promotion_is_deterministic_and_does_not_recompute_score() -> None:
    evaluation = _matched()
    descriptor = _descriptor()
    first = promote_evaluation(
        evaluation=evaluation,
        descriptor=descriptor,
        config_version="1.0.0",
        evaluation_timestamp=b.EVENT_TIME,
    )
    second = promote_evaluation(
        evaluation=evaluation,
        descriptor=descriptor,
        config_version="1.0.0",
        evaluation_timestamp=b.EVENT_TIME,
    )
    assert first == second
    assert first.score == evaluation.score  # carried through verbatim, never re-derived


def test_promotion_carries_a_no_match_without_reasons() -> None:
    evaluation = StrategyEvaluation(
        instrument=b.INSTRUMENT, context_version=1, status=EvaluationStatus.NO_MATCH
    )
    result = promote_evaluation(
        evaluation=evaluation,
        descriptor=_descriptor(),
        config_version="1.0.0",
        evaluation_timestamp=b.EVENT_TIME,
    )
    assert result.status is EvaluationStatus.NO_MATCH
    assert result.reason_codes == ()
    assert result.score is None
