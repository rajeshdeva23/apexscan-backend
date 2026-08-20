"""Immutability, validation, determinism, and Protocol conformance of P5.1 contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.market_engine.historical.requirements import HistoricalRequirement
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument
from app.strategies import (
    CandleCompleteness,
    EmissionPolicy,
    EvaluationStatus,
    FactNeed,
    MetricEntry,
    Strategy,
    StrategyCategory,
    StrategyConfiguration,
    StrategyDescriptor,
    StrategyEvaluation,
    StrategyEvaluationMetadata,
    StrategyLifecycleState,
    StrategyRequirements,
    StrategyResult,
    StrategyTrigger,
)

_INSTRUMENT = Instrument(exchange="NSE", symbol="RELIANCE")
_NOW = datetime(2026, 8, 6, 4, 0, tzinfo=UTC)


def _descriptor(**overrides: object) -> StrategyDescriptor:
    fields: dict[str, object] = {
        "strategy_id": "sample_strategy",
        "display_name": "Sample Strategy",
        "description": "A test descriptor.",
        "version": "1.0.0",
        "category": StrategyCategory.OPENING_SESSION,
        "emission_policy": EmissionPolicy.CONTINUOUS,
    }
    fields.update(overrides)
    return StrategyDescriptor(**fields)  # type: ignore[arg-type]


def _requirements(**overrides: object) -> StrategyRequirements:
    fields: dict[str, object] = {
        "trigger": StrategyTrigger.ON_CANDLE_FINALIZED,
        "candle_completeness": CandleCompleteness.AUTHORITATIVE_ONLY,
    }
    fields.update(overrides)
    return StrategyRequirements(**fields)  # type: ignore[arg-type]


class _SampleConfig(StrategyConfiguration):
    threshold: Decimal = Decimal("1")


class _SampleStrategy:
    @property
    def descriptor(self) -> StrategyDescriptor:
        return _descriptor()

    @property
    def requirements(self) -> StrategyRequirements:
        return _requirements()

    @property
    def configuration_type(self) -> type[StrategyConfiguration]:
        return _SampleConfig

    def evaluate(
        self,
        context: object,
        configuration: StrategyConfiguration,
        metadata: StrategyEvaluationMetadata,
    ) -> StrategyEvaluation:
        return StrategyEvaluation(
            instrument=_INSTRUMENT,
            context_version=metadata.context_version,
            status=EvaluationStatus.NO_MATCH,
        )


# --------------------------------------------------------------------------- #
# Protocol conformance & enums
# --------------------------------------------------------------------------- #
def test_sample_strategy_satisfies_protocol() -> None:
    assert isinstance(_SampleStrategy(), Strategy)


def test_enum_members_match_governance() -> None:
    assert set(EvaluationStatus) == {
        EvaluationStatus.MATCHED,
        EvaluationStatus.NO_MATCH,
        EvaluationStatus.SKIPPED,
        EvaluationStatus.ERROR,
    }
    assert set(EmissionPolicy) == {
        EmissionPolicy.CONTINUOUS,
        EmissionPolicy.EDGE_TRIGGERED,
        EmissionPolicy.ONE_SHOT_PER_SESSION,
    }
    assert set(CandleCompleteness) == {
        CandleCompleteness.AUTHORITATIVE_ONLY,
        CandleCompleteness.PARTIAL_ALLOWED,
    }
    assert len(set(StrategyLifecycleState)) == 7
    assert len(set(StrategyCategory)) == 10


# --------------------------------------------------------------------------- #
# Descriptor
# --------------------------------------------------------------------------- #
def test_descriptor_is_immutable() -> None:
    descriptor = _descriptor()
    with pytest.raises(ValidationError):
        descriptor.display_name = "Renamed"  # type: ignore[misc]


def test_descriptor_rejects_empty_id() -> None:
    with pytest.raises(ValidationError):
        _descriptor(strategy_id="")


def test_descriptor_rejects_non_machine_safe_id() -> None:
    with pytest.raises(ValidationError):
        _descriptor(strategy_id="Open High")


def test_descriptor_rejects_bad_version() -> None:
    with pytest.raises(ValidationError):
        _descriptor(version="v1")


def test_descriptor_rejects_inverted_context_range() -> None:
    with pytest.raises(ValidationError):
        _descriptor(min_context_version=5, max_context_version=2)


def test_descriptor_has_no_runtime_status_field() -> None:
    assert "status" not in StrategyDescriptor.model_fields
    assert "enabled" not in StrategyDescriptor.model_fields
    assert "state" not in StrategyDescriptor.model_fields


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_configuration_is_immutable() -> None:
    config = _SampleConfig(config_version="1.0.0")
    with pytest.raises(ValidationError):
        config.threshold = Decimal("2")  # type: ignore[misc]


def test_configuration_rejects_bad_version() -> None:
    with pytest.raises(ValidationError):
        _SampleConfig(config_version="latest")


# --------------------------------------------------------------------------- #
# Requirements — reuse + canonical normalization
# --------------------------------------------------------------------------- #
def test_requirements_reuse_phase4_contracts_and_dedup_order() -> None:
    reqs = _requirements(
        historical=(
            HistoricalRequirement(timeframe=Timeframe.minutes(15), lookback=20),
            HistoricalRequirement(timeframe=Timeframe.minutes(5), lookback=100),
            HistoricalRequirement(timeframe=Timeframe.minutes(5), lookback=100),  # dup
        ),
        live_timeframes=(Timeframe.minutes(15), Timeframe.minutes(5), Timeframe.minutes(5)),
        fact_needs=(FactNeed.SESSION, FactNeed.PREVIOUS_SESSION, FactNeed.SESSION),
    )
    assert [r.timeframe for r in reqs.historical] == [Timeframe.minutes(5), Timeframe.minutes(15)]
    assert reqs.live_timeframes == (Timeframe.minutes(5), Timeframe.minutes(15))
    assert reqs.fact_needs == (FactNeed.PREVIOUS_SESSION, FactNeed.SESSION)


def test_requirements_are_immutable() -> None:
    reqs = _requirements()
    with pytest.raises(ValidationError):
        reqs.live_timeframes = (Timeframe.minutes(1),)  # type: ignore[misc]


def test_requirements_default_to_empty_collections() -> None:
    reqs = _requirements()
    assert reqs.historical == () and reqs.live_timeframes == () and reqs.fact_needs == ()


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def test_evaluation_is_immutable() -> None:
    evaluation = StrategyEvaluation(
        instrument=_INSTRUMENT, context_version=1, status=EvaluationStatus.NO_MATCH
    )
    with pytest.raises(ValidationError):
        evaluation.status = EvaluationStatus.MATCHED  # type: ignore[misc]


def test_matched_evaluation_requires_reason_codes() -> None:
    with pytest.raises(ValidationError, match="reason code"):
        StrategyEvaluation(
            instrument=_INSTRUMENT, context_version=1, status=EvaluationStatus.MATCHED
        )


def test_evaluation_rejects_non_positive_context_version() -> None:
    with pytest.raises(ValidationError):
        StrategyEvaluation(
            instrument=_INSTRUMENT, context_version=0, status=EvaluationStatus.NO_MATCH
        )


def test_reason_codes_reject_duplicates_and_bad_format() -> None:
    with pytest.raises(ValidationError, match="duplicates"):
        StrategyEvaluation(
            instrument=_INSTRUMENT,
            context_version=1,
            status=EvaluationStatus.MATCHED,
            reason_codes=("OPEN_EQUALS_HIGH", "OPEN_EQUALS_HIGH"),
        )
    with pytest.raises(ValidationError):
        StrategyEvaluation(
            instrument=_INSTRUMENT,
            context_version=1,
            status=EvaluationStatus.MATCHED,
            reason_codes=("open equals high",),
        )


def test_reason_code_order_is_preserved() -> None:
    evaluation = StrategyEvaluation(
        instrument=_INSTRUMENT,
        context_version=1,
        status=EvaluationStatus.MATCHED,
        reason_codes=("B_REASON", "A_REASON"),
    )
    assert evaluation.reason_codes == ("B_REASON", "A_REASON")


def test_metrics_are_unique_and_canonically_sorted() -> None:
    evaluation = StrategyEvaluation(
        instrument=_INSTRUMENT,
        context_version=1,
        status=EvaluationStatus.NO_MATCH,
        metrics=(
            MetricEntry(name="high", value=Decimal("101")),
            MetricEntry(name="close", value=Decimal("100")),
        ),
    )
    assert [m.name for m in evaluation.metrics] == ["close", "high"]


def test_duplicate_metric_names_rejected() -> None:
    with pytest.raises(ValidationError, match="unique"):
        StrategyEvaluation(
            instrument=_INSTRUMENT,
            context_version=1,
            status=EvaluationStatus.NO_MATCH,
            metrics=(MetricEntry(name="x", value=1), MetricEntry(name="x", value=2)),
        )


def test_metric_rejects_float_value_in_strict_mode() -> None:
    with pytest.raises(ValidationError):
        MetricEntry(name="x", value=1.5)  # type: ignore[arg-type]


def test_score_has_no_governed_range() -> None:
    evaluation = StrategyEvaluation(
        instrument=_INSTRUMENT,
        context_version=1,
        status=EvaluationStatus.MATCHED,
        reason_codes=("R",),
        score=Decimal("150"),
        confidence=Decimal("0.9"),
    )
    assert evaluation.score == Decimal("150")


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #
def _result(**overrides: object) -> StrategyResult:
    fields: dict[str, object] = {
        "strategy_id": "sample_strategy",
        "strategy_version": "1.0.0",
        "config_version": "1.0.0",
        "instrument": _INSTRUMENT,
        "context_version": 1,
        "evaluation_timestamp": _NOW,
        "status": EvaluationStatus.MATCHED,
        "reason_codes": ("OPEN_EQUALS_HIGH",),
    }
    fields.update(overrides)
    return StrategyResult(**fields)  # type: ignore[arg-type]


def test_result_is_immutable() -> None:
    result = _result()
    with pytest.raises(ValidationError):
        result.status = EvaluationStatus.NO_MATCH  # type: ignore[misc]


def test_result_has_no_rank_field() -> None:
    assert "rank" not in StrategyResult.model_fields


def test_result_result_id_defaults_to_none_no_random() -> None:
    assert _result().result_id is None


def test_result_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _result(evaluation_timestamp=datetime(2026, 8, 6, 4, 0))  # noqa: DTZ001


def test_matched_result_requires_reasons() -> None:
    with pytest.raises(ValidationError, match="reason code"):
        _result(reason_codes=())


def test_result_uses_canonical_instrument() -> None:
    assert isinstance(_result().instrument, Instrument)


def test_result_has_no_provider_fields() -> None:
    forbidden = {"security_id", "securityId", "dhan_security_id", "provider", "exchange_segment"}
    assert forbidden.isdisjoint(StrategyResult.model_fields)


# --------------------------------------------------------------------------- #
# Determinism & serialization
# --------------------------------------------------------------------------- #
def test_equal_inputs_produce_equal_results() -> None:
    assert _result() == _result()
    assert _result().model_dump() == _result().model_dump()


def test_result_round_trips_through_json() -> None:
    result = _result(score=Decimal("42"), metrics=(MetricEntry(name="open", value=Decimal("100")),))
    assert StrategyResult.model_validate_json(result.model_dump_json()) == result


def test_requirements_serialization_is_deterministic() -> None:
    reqs = _requirements(fact_needs=(FactNeed.SESSION, FactNeed.LATEST_TICK))
    assert (
        reqs.model_dump()
        == _requirements(fact_needs=(FactNeed.LATEST_TICK, FactNeed.SESSION)).model_dump()
    )


# --------------------------------------------------------------------------- #
# Evaluation metadata
# --------------------------------------------------------------------------- #
def test_evaluation_metadata_rejects_naive_observed_at() -> None:
    with pytest.raises(ValidationError):
        StrategyEvaluationMetadata(
            trigger=StrategyTrigger.ON_TICK,
            context_version=1,
            observed_at=datetime(2026, 8, 6, 4, 0),  # noqa: DTZ001
        )


def test_evaluation_metadata_accepts_trading_date() -> None:
    meta = StrategyEvaluationMetadata(
        trigger=StrategyTrigger.ON_CONTEXT,
        context_version=3,
        observed_at=_NOW,
        trading_date=date(2026, 8, 6),
    )
    assert meta.context_version == 3 and meta.trading_date == date(2026, 8, 6)
