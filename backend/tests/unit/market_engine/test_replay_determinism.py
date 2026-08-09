"""Deterministic-replay ordering test for the P4.1 foundation (docs/06 §1.4, §26).

No market logic is exercised — only that the foundation primitives (immutable
context, monotonic version/sequence, injected clock, ordered event bus) produce
identical output when identical scripted inputs are replayed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.events.bus import EventBus
from app.market_engine.clock import ManualClock
from app.market_engine.context import MarketContext
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.sequence import MonotonicSequence
from app.schemas.market_data import Instrument

_START = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)
_EVENT_TIME = datetime(2026, 8, 6, 6, 29, tzinfo=UTC)
_UPDATES = 4


def _run() -> list[tuple[str, int, int, str]]:
    """Replay a fixed script and return the ordered (event, version, seq, time) log."""
    clock = ManualClock(_START)
    sequence = MonotonicSequence()
    bus = EventBus()
    log: list[tuple[str, int, int, str]] = []

    def record(event: MarketContextCreated | MarketContextUpdated) -> None:
        kind = type(event).__name__
        context = event.context
        log.append((kind, context.version, context.sequence, context.observed_at.isoformat()))

    bus.subscribe(MarketContextCreated, record)
    bus.subscribe(MarketContextUpdated, record)

    instrument = Instrument(exchange="NSE", symbol="RELIANCE")
    context = MarketContext.initial(
        instrument,
        sequence=sequence.next_value(),
        event_timestamp=_EVENT_TIME,
        observed_at=clock.now(),
    )
    bus.publish(MarketContextCreated(context=context))

    for _ in range(_UPDATES):
        clock.advance(timedelta(seconds=1))
        previous = context.version
        context = context.with_update(
            sequence=sequence.next_value(),
            event_timestamp=_EVENT_TIME,
            observed_at=clock.now(),
        )
        bus.publish(MarketContextUpdated(context=context, previous_version=previous))

    return log


def test_replay_produces_identical_ordered_output() -> None:
    assert _run() == _run()


def test_replay_versions_and_sequences_are_monotonic() -> None:
    log = _run()
    versions = [version for _, version, _, _ in log]
    sequences = [sequence for _, _, sequence, _ in log]
    assert versions == [1, 2, 3, 4, 5]
    assert sequences == [1, 2, 3, 4, 5]


def test_replay_first_event_is_created_then_updates() -> None:
    kinds = [kind for kind, _, _, _ in _run()]
    assert kinds == [
        "MarketContextCreated",
        "MarketContextUpdated",
        "MarketContextUpdated",
        "MarketContextUpdated",
        "MarketContextUpdated",
    ]
