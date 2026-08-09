"""Tests for the in-process synchronous event bus (docs/03 §14, docs/09 §15)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.events.bus import EventBus
from app.market_engine.context import MarketContext
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.schemas.market_data import Instrument

_EVENT_TIME = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)


def _context(version_seed: int = 1) -> MarketContext:
    return MarketContext.initial(
        Instrument(exchange="NSE", symbol="RELIANCE"),
        sequence=version_seed,
        event_timestamp=_EVENT_TIME,
        observed_at=_EVENT_TIME,
    )


def test_publish_delivers_to_a_subscriber() -> None:
    bus = EventBus()
    received: list[MarketContextCreated] = []
    bus.subscribe(MarketContextCreated, received.append)

    event = MarketContextCreated(context=_context())
    bus.publish(event)

    assert received == [event]


def test_multiple_subscribers_are_called_in_subscription_order() -> None:
    bus = EventBus()
    order: list[str] = []
    bus.subscribe(MarketContextCreated, lambda _event: order.append("first"))
    bus.subscribe(MarketContextCreated, lambda _event: order.append("second"))

    bus.publish(MarketContextCreated(context=_context()))

    assert order == ["first", "second"]


def test_events_are_delivered_in_publish_order() -> None:
    bus = EventBus()
    seen: list[int] = []
    bus.subscribe(MarketContextUpdated, lambda event: seen.append(event.context.version))

    context = _context()
    for _ in range(3):
        context = context.with_update(
            sequence=context.sequence + 1,
            event_timestamp=_EVENT_TIME,
            observed_at=_EVENT_TIME,
        )
        bus.publish(MarketContextUpdated(context=context, previous_version=context.version - 1))

    assert seen == [2, 3, 4]


def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    received: list[MarketContextCreated] = []
    subscription = bus.subscribe(MarketContextCreated, received.append)

    bus.unsubscribe(subscription)
    bus.publish(MarketContextCreated(context=_context()))

    assert received == []


def test_unsubscribe_is_idempotent() -> None:
    bus = EventBus()
    subscription = bus.subscribe(MarketContextCreated, lambda _event: None)
    bus.unsubscribe(subscription)
    bus.unsubscribe(subscription)  # must not raise


def test_dispatch_is_by_exact_event_type() -> None:
    bus = EventBus()
    created: list[MarketContextCreated] = []
    bus.subscribe(MarketContextCreated, created.append)

    bus.publish(MarketContextUpdated(context=_context(), previous_version=0))

    assert created == []


def test_publish_does_not_mutate_the_payload() -> None:
    bus = EventBus()
    bus.subscribe(MarketContextUpdated, lambda _event: None)
    context = _context()
    event = MarketContextUpdated(context=context, previous_version=0)

    bus.publish(event)

    assert event.context is context
    assert event.context.version == 1
    with pytest.raises(ValidationError):
        event.context.version = 99  # type: ignore[misc]


def test_subscriber_added_during_dispatch_is_not_called_for_current_event() -> None:
    bus = EventBus()
    late_calls: list[MarketContextCreated] = []

    def add_late_subscriber(_event: MarketContextCreated) -> None:
        bus.subscribe(MarketContextCreated, late_calls.append)

    bus.subscribe(MarketContextCreated, add_late_subscriber)
    bus.publish(MarketContextCreated(context=_context()))

    assert late_calls == []
