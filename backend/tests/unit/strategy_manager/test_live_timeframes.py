"""LiveTimeframeRequirementRegistry: union, sharing, determinism, immutability (P5.4)."""

from __future__ import annotations

import pytest

from app.market_engine.timeframe import Timeframe
from app.strategy_manager.live_timeframes import LiveTimeframeRequirementRegistry

_M5 = Timeframe.minutes(5)
_M15 = Timeframe.minutes(15)
_M30 = Timeframe.minutes(30)


def test_empty_registry_has_no_effective_timeframes() -> None:
    registry = LiveTimeframeRequirementRegistry()
    assert registry.effective_timeframes() == frozenset()
    assert registry.snapshot() == ()


def test_single_consumer_effective_set_is_its_timeframes() -> None:
    registry = LiveTimeframeRequirementRegistry()
    registry.register("a", (_M5, _M15))
    assert registry.effective_timeframes() == frozenset({_M5, _M15})
    assert registry.requirements_for("a") == frozenset({_M5, _M15})


def test_duplicate_timeframes_collapse() -> None:
    registry = LiveTimeframeRequirementRegistry()
    registry.register("a", (_M5, _M5, _M15))
    assert registry.effective_timeframes() == frozenset({_M5, _M15})


def test_multiple_consumers_union() -> None:
    registry = LiveTimeframeRequirementRegistry()
    registry.register("a", (_M5,))
    registry.register("b", (_M5, _M15))
    assert registry.effective_timeframes() == frozenset({_M5, _M15})


def test_shared_timeframe_survives_one_consumer_deregistration() -> None:
    registry = LiveTimeframeRequirementRegistry()
    registry.register("a", (_M5,))
    registry.register("b", (_M5, _M15))
    registry.deregister("a")  # b still needs 5m
    assert registry.effective_timeframes() == frozenset({_M5, _M15})
    registry.deregister("b")
    assert registry.effective_timeframes() == frozenset()


def test_reregistration_replaces_rather_than_accumulates() -> None:
    registry = LiveTimeframeRequirementRegistry()
    registry.register("a", (_M5, _M15))
    registry.register("a", (_M30,))  # replaces
    assert registry.requirements_for("a") == frozenset({_M30})
    assert registry.effective_timeframes() == frozenset({_M30})


def test_deregistering_unknown_consumer_is_a_noop() -> None:
    registry = LiveTimeframeRequirementRegistry()
    registry.register("a", (_M5,))
    registry.deregister("ghost")  # no error, no effect
    assert registry.effective_timeframes() == frozenset({_M5})


def test_requirements_for_unknown_consumer_is_empty() -> None:
    assert LiveTimeframeRequirementRegistry().requirements_for("ghost") == frozenset()


def test_empty_consumer_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        LiveTimeframeRequirementRegistry().register("  ", (_M5,))


def test_snapshot_is_deterministic_and_registration_order_independent() -> None:
    forward = LiveTimeframeRequirementRegistry()
    forward.register("b", (_M15, _M5))
    forward.register("a", (_M30,))

    reverse = LiveTimeframeRequirementRegistry()
    reverse.register("a", (_M30,))
    reverse.register("b", (_M5, _M15))

    expected = (("a", (_M30,)), ("b", (_M5, _M15)))
    assert forward.snapshot() == expected
    assert forward.snapshot() == reverse.snapshot()


def test_effective_union_is_registration_order_independent() -> None:
    forward = LiveTimeframeRequirementRegistry()
    forward.register("a", (_M5,))
    forward.register("b", (_M15,))
    reverse = LiveTimeframeRequirementRegistry()
    reverse.register("b", (_M15,))
    reverse.register("a", (_M5,))
    assert forward.effective_timeframes() == reverse.effective_timeframes()


def test_exposed_snapshots_are_immutable() -> None:
    registry = LiveTimeframeRequirementRegistry()
    registry.register("a", (_M5,))
    assert isinstance(registry.effective_timeframes(), frozenset)
    assert isinstance(registry.requirements_for("a"), frozenset)
    assert isinstance(registry.snapshot(), tuple)
