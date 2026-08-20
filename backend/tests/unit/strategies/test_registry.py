"""StrategyRegistry behaviour: generic, deterministic, fail-closed (P5.2 Part 1)."""

from __future__ import annotations

import pytest

from app.schemas.market_data import Instrument
from app.strategies import (
    EmissionPolicy,
    EvaluationStatus,
    InvalidStrategyError,
    StrategyAlreadyRegisteredError,
    StrategyCategory,
    StrategyConfiguration,
    StrategyDescriptor,
    StrategyEvaluation,
    StrategyEvaluationMetadata,
    StrategyNotFoundError,
    StrategyRegistry,
    StrategyRequirements,
    StrategyTrigger,
)
from app.strategies.enums import CandleCompleteness

_INSTRUMENT = Instrument(exchange="NSE", symbol="RELIANCE")


class _FakeStrategy:
    """A minimal, contract-conforming strategy with an arbitrary id (test-only)."""

    def __init__(self, strategy_id: str) -> None:
        self._id = strategy_id

    @property
    def descriptor(self) -> StrategyDescriptor:
        return StrategyDescriptor(
            strategy_id=self._id,
            display_name=f"Fake {self._id}",
            description="A test strategy.",
            version="1.0.0",
            category=StrategyCategory.MOMENTUM,
            emission_policy=EmissionPolicy.CONTINUOUS,
        )

    @property
    def requirements(self) -> StrategyRequirements:
        return StrategyRequirements(
            trigger=StrategyTrigger.ON_CONTEXT,
            candle_completeness=CandleCompleteness.AUTHORITATIVE_ONLY,
        )

    @property
    def configuration_type(self) -> type[StrategyConfiguration]:
        return StrategyConfiguration

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


class _NotAStrategy:
    """Missing the evaluate method — must fail the Protocol check."""

    @property
    def descriptor(self) -> object:
        return object()


def test_register_and_get_returns_exact_instance() -> None:
    registry = StrategyRegistry()
    strategy = _FakeStrategy("alpha")
    registry.register(strategy)
    assert registry.get("alpha") is strategy


def test_descriptor_id_is_the_registration_key() -> None:
    registry = StrategyRegistry()
    registry.register(_FakeStrategy("beta"))
    assert registry.contains("beta")
    assert not registry.contains("alpha")


def test_duplicate_id_is_rejected_even_for_same_instance() -> None:
    registry = StrategyRegistry()
    strategy = _FakeStrategy("gamma")
    registry.register(strategy)
    with pytest.raises(StrategyAlreadyRegisteredError, match="gamma"):
        registry.register(strategy)
    with pytest.raises(StrategyAlreadyRegisteredError):
        registry.register(_FakeStrategy("gamma"))  # different instance, same id


def test_unknown_lookup_raises_typed_error() -> None:
    with pytest.raises(StrategyNotFoundError, match="missing"):
        StrategyRegistry().get("missing")


def test_non_conforming_object_is_rejected() -> None:
    with pytest.raises(InvalidStrategyError):
        StrategyRegistry().register(_NotAStrategy())  # type: ignore[arg-type]


def test_listing_is_deterministic_and_order_independent() -> None:
    forward = StrategyRegistry()
    forward.register(_FakeStrategy("b_strategy"))
    forward.register(_FakeStrategy("a_strategy"))
    forward.register(_FakeStrategy("c_strategy"))

    reverse = StrategyRegistry()
    reverse.register(_FakeStrategy("c_strategy"))
    reverse.register(_FakeStrategy("a_strategy"))
    reverse.register(_FakeStrategy("b_strategy"))

    assert forward.identifiers() == ("a_strategy", "b_strategy", "c_strategy")
    assert forward.identifiers() == reverse.identifiers()
    assert [s.descriptor.strategy_id for s in forward.strategies()] == list(forward.identifiers())


def test_snapshot_is_an_immutable_tuple() -> None:
    registry = StrategyRegistry()
    registry.register(_FakeStrategy("alpha"))
    assert isinstance(registry.strategies(), tuple)
    assert isinstance(registry.identifiers(), tuple)


def test_registry_is_generic_across_unrelated_strategies() -> None:
    registry = StrategyRegistry()
    for strategy_id in ("open_high_like", "narrow_range_like", "gap_like"):
        registry.register(_FakeStrategy(strategy_id))
    assert len(registry.strategies()) == 3
    assert registry.get("gap_like").descriptor.category is StrategyCategory.MOMENTUM
