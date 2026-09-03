"""Tests for the immutable, versioned MarketContext (docs/06 §6)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.market_engine.clock import ManualClock
from app.market_engine.context import MarketContext, MarketFact
from app.market_engine.sequence import MonotonicSequence
from app.schemas.market_data import Instrument, Tick

_EVENT_TIME = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)


def _instrument() -> Instrument:
    return Instrument(exchange="NSE", symbol="RELIANCE")


def _initial(clock: ManualClock, sequence: MonotonicSequence) -> MarketContext:
    return MarketContext.initial(
        _instrument(),
        sequence=sequence.next_value(),
        event_timestamp=_EVENT_TIME,
        observed_at=clock.now(),
    )


def test_initial_context_is_version_one() -> None:
    context = _initial(ManualClock(_EVENT_TIME), MonotonicSequence())
    assert context.version == 1
    assert context.sequence == 1
    assert context.is_valid is True


def test_context_is_immutable() -> None:
    context = _initial(ManualClock(_EVENT_TIME), MonotonicSequence())
    with pytest.raises(ValidationError):
        context.version = 99  # type: ignore[misc]


def test_facts_collection_is_an_immutable_tuple() -> None:
    fact = MarketFact(name="prev_close", value=Decimal("100.5"))
    context = MarketContext.initial(
        _instrument(),
        sequence=1,
        event_timestamp=_EVENT_TIME,
        observed_at=_EVENT_TIME,
        facts=(fact,),
    )
    assert context.facts == (fact,)
    assert isinstance(context.facts, tuple)


def test_with_update_increments_version_by_exactly_one() -> None:
    clock = ManualClock(_EVENT_TIME)
    sequence = MonotonicSequence()
    context = _initial(clock, sequence)

    updated = context.with_update(
        sequence=sequence.next_value(),
        event_timestamp=_EVENT_TIME,
        observed_at=clock.now(),
    )

    assert updated.version == 2
    assert updated.sequence == 2


def test_with_update_does_not_mutate_the_original() -> None:
    clock = ManualClock(_EVENT_TIME)
    sequence = MonotonicSequence()
    context = _initial(clock, sequence)

    context.with_update(
        sequence=sequence.next_value(),
        event_timestamp=_EVENT_TIME,
        observed_at=clock.now(),
    )

    assert context.version == 1
    assert context.sequence == 1


def test_versions_never_skip_or_decrease_across_a_chain() -> None:
    clock = ManualClock(_EVENT_TIME)
    sequence = MonotonicSequence()
    context = _initial(clock, sequence)
    versions = [context.version]

    for _ in range(5):
        context = context.with_update(
            sequence=sequence.next_value(),
            event_timestamp=_EVENT_TIME,
            observed_at=clock.now(),
        )
        versions.append(context.version)

    assert versions == [1, 2, 3, 4, 5, 6]


def test_observed_at_comes_from_the_injected_clock() -> None:
    clock = ManualClock(datetime(2026, 8, 6, 9, 15, tzinfo=UTC))
    context = _initial(clock, MonotonicSequence())
    assert context.observed_at == datetime(2026, 8, 6, 9, 15, tzinfo=UTC)


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError):
        MarketContext.initial(
            _instrument(),
            sequence=1,
            event_timestamp=datetime(2026, 8, 6, 6, 30),  # noqa: DTZ001 (intentionally naive)
            observed_at=_EVENT_TIME,
        )


def test_context_carries_optional_data_slots() -> None:
    tick = Tick(instrument=_instrument(), event_timestamp=_EVENT_TIME, last_price=Decimal("101.25"))
    context = MarketContext.initial(
        _instrument(),
        sequence=1,
        event_timestamp=_EVENT_TIME,
        observed_at=_EVENT_TIME,
        latest_tick=tick,
    )
    assert context.latest_tick == tick
    assert context.latest_quote is None
    assert context.latest_candle is None


def test_previous_close_defaults_to_none() -> None:
    context = _initial(ManualClock(_EVENT_TIME), MonotonicSequence())
    assert context.previous_close is None


def test_with_update_sets_and_clears_previous_close() -> None:
    sequence = MonotonicSequence()
    context = _initial(ManualClock(_EVENT_TIME), sequence)
    with_reference = context.with_update(
        sequence=sequence.next_value(),
        event_timestamp=_EVENT_TIME,
        observed_at=_EVENT_TIME,
        previous_close=Decimal("100"),
    )
    assert with_reference.previous_close == Decimal("100")
    assert context.previous_close is None  # prior snapshot is untouched

    cleared = with_reference.with_update(
        sequence=sequence.next_value(),
        event_timestamp=_EVENT_TIME,
        observed_at=_EVENT_TIME,
    )
    assert cleared.previous_close is None


def test_previous_close_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        MarketContext.initial(
            _instrument(),
            sequence=1,
            event_timestamp=_EVENT_TIME,
            observed_at=_EVENT_TIME,
            previous_close=Decimal("0"),
        )
