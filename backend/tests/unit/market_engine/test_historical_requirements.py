"""Historical requirement model and deterministic union (P4.5A; §29)."""

from __future__ import annotations

import dataclasses

import pytest

from app.market_engine.historical.requirements import (
    HistoricalRequirement,
    HistoricalRequirementRegistry,
)
from app.market_engine.timeframe import Timeframe


def _req(minutes: int, lookback: int) -> HistoricalRequirement:
    return HistoricalRequirement(timeframe=Timeframe.minutes(minutes), lookback=lookback)


def test_requirement_is_immutable() -> None:
    requirement = _req(5, 50)
    with pytest.raises(dataclasses.FrozenInstanceError):
        requirement.lookback = 60  # type: ignore[misc]


def test_requirement_is_hashable() -> None:
    assert _req(5, 50) in {_req(5, 50)}


def test_zero_lookback_rejected() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        _req(5, 0)


def test_negative_lookback_rejected() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        _req(5, -1)


def test_duplicate_requirement_collapses_within_a_consumer() -> None:
    registry = HistoricalRequirementRegistry()
    registry.register("a", [_req(5, 50), _req(5, 50)])
    assert registry.effective_requirements() == (_req(5, 50),)


def test_max_lookback_union_across_consumers() -> None:
    registry = HistoricalRequirementRegistry()
    registry.register("a", [_req(5, 50)])
    registry.register("b", [_req(5, 100)])
    registry.register("c", [_req(5, 20)])
    assert registry.effective_requirements() == (_req(5, 100),)


def test_multi_timeframe_union() -> None:
    registry = HistoricalRequirementRegistry()
    registry.register("a", [_req(5, 50), _req(15, 20)])
    registry.register("b", [_req(5, 100)])
    registry.register("c", [HistoricalRequirement(timeframe=Timeframe.session(), lookback=10)])
    assert registry.effective_requirements() == (
        _req(5, 100),
        _req(15, 20),
        HistoricalRequirement(timeframe=Timeframe.session(), lookback=10),
    )


def test_max_lookback_union_for_same_timeframe_within_one_consumer() -> None:
    registry = HistoricalRequirementRegistry()
    registry.register("a", [_req(5, 50), _req(5, 20)])
    assert registry.effective_requirements() == (_req(5, 50),)


def test_re_register_replaces_consumer_requirements() -> None:
    registry = HistoricalRequirementRegistry()
    registry.register("a", [_req(5, 100)])
    registry.register("a", [_req(5, 10)])
    assert registry.effective_requirements() == (_req(5, 10),)


def test_deregister_removes_a_consumer() -> None:
    registry = HistoricalRequirementRegistry()
    registry.register("a", [_req(5, 100)])
    registry.register("b", [_req(5, 20)])
    registry.deregister("a")
    assert registry.effective_requirements() == (_req(5, 20),)


def test_deregister_is_idempotent() -> None:
    registry = HistoricalRequirementRegistry()
    registry.deregister("missing")  # no error
    registry.register("a", [_req(5, 20)])
    registry.deregister("a")
    registry.deregister("a")  # again, still no error
    assert registry.effective_requirements() == ()


def test_empty_consumer_key_rejected() -> None:
    registry = HistoricalRequirementRegistry()
    with pytest.raises(ValueError, match="non-empty"):
        registry.register("   ", [_req(5, 20)])


def test_seven_minute_requirement_is_valid_without_provider_capability() -> None:
    registry = HistoricalRequirementRegistry()
    registry.register("a", [_req(7, 30)])
    registry.register("b", [_req(7, 70)])
    assert registry.effective_requirements() == (_req(7, 70),)


def test_effective_ordering_is_deterministic_across_timeframes() -> None:
    registry = HistoricalRequirementRegistry()
    registry.register(
        "a",
        [
            HistoricalRequirement(timeframe=Timeframe.session(), lookback=5),
            _req(15, 5),
            _req(1, 5),
            _req(7, 5),
            _req(5, 5),
        ],
    )
    ordered = [r.timeframe for r in registry.effective_requirements()]
    assert ordered == [
        Timeframe.minutes(1),
        Timeframe.minutes(5),
        Timeframe.minutes(7),
        Timeframe.minutes(15),
        Timeframe.session(),
    ]


def test_registration_order_does_not_change_effective_result() -> None:
    first = HistoricalRequirementRegistry()
    first.register("a", [_req(5, 50)])
    first.register("b", [_req(15, 20)])
    first.register("c", [_req(1, 5)])

    second = HistoricalRequirementRegistry()
    second.register("c", [_req(1, 5)])
    second.register("a", [_req(5, 50)])
    second.register("b", [_req(15, 20)])

    assert first.effective_requirements() == second.effective_requirements()


def test_consumer_key_does_not_leak_into_effective_requirements() -> None:
    registry = HistoricalRequirementRegistry()
    registry.register("strategy-secret-name", [_req(5, 50)])
    effective = registry.effective_requirements()
    assert all(not hasattr(r, "consumer_key") for r in effective)
    assert set(dataclasses.asdict(effective[0])) == {"timeframe", "lookback"}
