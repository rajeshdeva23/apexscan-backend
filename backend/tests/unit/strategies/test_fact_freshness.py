"""FactNeed.SESSION_STATISTICS and the FactFreshnessRequirement contract (P4.6E5; ADR-009 D6)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.strategies.enums import CandleCompleteness, FactNeed, StrategyTrigger
from app.strategies.requirements import FactFreshnessRequirement, StrategyRequirements

_TRIGGER = StrategyTrigger.ON_TICK
_COMPLETENESS = CandleCompleteness.PARTIAL_ALLOWED


def _requirements(**kwargs: object) -> StrategyRequirements:
    return StrategyRequirements(trigger=_TRIGGER, candle_completeness=_COMPLETENESS, **kwargs)  # type: ignore[arg-type]


def test_session_statistics_fact_need_exists() -> None:
    assert FactNeed.SESSION_STATISTICS.value == "session_statistics"


def test_existing_requirements_default_to_empty_freshness() -> None:
    assert _requirements().freshness == ()


def test_positive_max_age_is_valid() -> None:
    entry = FactFreshnessRequirement(fact=FactNeed.SESSION_STATISTICS, max_age=timedelta(seconds=5))
    assert entry.max_age == timedelta(seconds=5)


def test_zero_max_age_is_rejected() -> None:
    with pytest.raises(ValidationError):
        FactFreshnessRequirement(fact=FactNeed.SESSION_STATISTICS, max_age=timedelta(0))


def test_negative_max_age_is_rejected() -> None:
    with pytest.raises(ValidationError):
        FactFreshnessRequirement(fact=FactNeed.SESSION_STATISTICS, max_age=timedelta(seconds=-1))


def test_freshness_is_immutable() -> None:
    entry = FactFreshnessRequirement(fact=FactNeed.SESSION_STATISTICS, max_age=timedelta(seconds=5))
    with pytest.raises(ValidationError):
        entry.max_age = timedelta(seconds=9)  # type: ignore[misc]


def test_freshness_for_undeclared_fact_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _requirements(
            fact_needs=(FactNeed.LATEST_TICK,),
            freshness=(
                FactFreshnessRequirement(
                    fact=FactNeed.SESSION_STATISTICS, max_age=timedelta(seconds=5)
                ),
            ),
        )


def test_duplicate_fact_freshness_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _requirements(
            fact_needs=(FactNeed.SESSION_STATISTICS,),
            freshness=(
                FactFreshnessRequirement(
                    fact=FactNeed.SESSION_STATISTICS, max_age=timedelta(seconds=5)
                ),
                FactFreshnessRequirement(
                    fact=FactNeed.SESSION_STATISTICS, max_age=timedelta(seconds=3)
                ),
            ),
        )


def test_freshness_is_canonically_ordered() -> None:
    requirements = _requirements(
        fact_needs=(FactNeed.SESSION_STATISTICS, FactNeed.LATEST_TICK),
        freshness=(
            FactFreshnessRequirement(
                fact=FactNeed.SESSION_STATISTICS, max_age=timedelta(seconds=5)
            ),
            FactFreshnessRequirement(fact=FactNeed.LATEST_TICK, max_age=timedelta(seconds=2)),
        ),
    )
    assert [entry.fact for entry in requirements.freshness] == [
        FactNeed.LATEST_TICK,
        FactNeed.SESSION_STATISTICS,
    ]


def test_declared_session_statistics_with_freshness_is_valid() -> None:
    requirements = _requirements(
        fact_needs=(FactNeed.SESSION,) + (FactNeed.SESSION_STATISTICS,),
        freshness=(
            FactFreshnessRequirement(
                fact=FactNeed.SESSION_STATISTICS, max_age=timedelta(seconds=5)
            ),
        ),
    )
    assert requirements.freshness[0].fact is FactNeed.SESSION_STATISTICS


def test_requirements_serialization_round_trip_preserves_freshness() -> None:
    requirements = _requirements(
        fact_needs=(FactNeed.SESSION_STATISTICS,),
        freshness=(
            FactFreshnessRequirement(
                fact=FactNeed.SESSION_STATISTICS, max_age=timedelta(seconds=5)
            ),
        ),
    )
    assert StrategyRequirements.model_validate(requirements.model_dump()) == requirements
