"""FactRequirementRegistry: session-statistics demand union and strictest freshness (P4.6E5)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.strategy_manager.fact_requirements import FactRequirementRegistry

_5S = timedelta(seconds=5)
_3S = timedelta(seconds=3)
_10S = timedelta(seconds=10)


def test_empty_registry_is_inactive() -> None:
    registry = FactRequirementRegistry()
    assert registry.is_active() is False
    assert registry.effective_session_statistics_max_age() is None


def test_first_consumer_activates() -> None:
    registry = FactRequirementRegistry()
    registry.register("a", session_statistics_max_age=_10S)
    assert registry.is_active() is True
    assert registry.effective_session_statistics_max_age() == _10S


def test_strictest_max_age_wins() -> None:
    registry = FactRequirementRegistry()
    registry.register("a", session_statistics_max_age=_10S)
    registry.register("b", session_statistics_max_age=_3S)
    assert registry.effective_session_statistics_max_age() == _3S  # min, never max


def test_registration_order_is_independent() -> None:
    forward = FactRequirementRegistry()
    forward.register("a", session_statistics_max_age=_10S)
    forward.register("b", session_statistics_max_age=_3S)
    reverse = FactRequirementRegistry()
    reverse.register("b", session_statistics_max_age=_3S)
    reverse.register("a", session_statistics_max_age=_10S)
    assert (
        forward.effective_session_statistics_max_age()
        == reverse.effective_session_statistics_max_age()
        == _3S
    )


def test_none_max_age_does_not_activate() -> None:
    registry = FactRequirementRegistry()
    registry.register("a", session_statistics_max_age=None)
    assert registry.is_active() is False


def test_reregistering_with_none_removes_the_consumer() -> None:
    registry = FactRequirementRegistry()
    registry.register("a", session_statistics_max_age=_5S)
    registry.register("a", session_statistics_max_age=None)  # e.g. restart without the fact
    assert registry.is_active() is False


def test_shared_demand_survives_one_deregistration() -> None:
    registry = FactRequirementRegistry()
    registry.register("a", session_statistics_max_age=_10S)
    registry.register("b", session_statistics_max_age=_3S)
    registry.deregister("b")  # strictest consumer leaves
    assert registry.effective_session_statistics_max_age() == _10S  # relaxes to A's bound
    registry.deregister("a")
    assert registry.effective_session_statistics_max_age() is None  # inactive


def test_deregistering_unknown_consumer_is_a_noop() -> None:
    registry = FactRequirementRegistry()
    registry.register("a", session_statistics_max_age=_5S)
    registry.deregister("ghost")
    assert registry.effective_session_statistics_max_age() == _5S


def test_empty_consumer_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        FactRequirementRegistry().register("  ", session_statistics_max_age=_5S)
