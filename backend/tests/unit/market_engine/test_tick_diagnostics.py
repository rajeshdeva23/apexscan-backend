"""FIX-1: bounded, behavior-preserving TickEngine accept/reject diagnostics.

These prove the diagnostics observe the engine's *existing* decision (never recompute or
change it), expose the decisive ``event_timestamp - now`` delta at a rejection, keep the
tick invariant ``received == accepted + rejected``, and stay O(1) in memory.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.events.bus import Event, EventBus
from app.market_engine.clock import ManualClock
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.tick_engine import TickEngine
from app.market_engine.validation import ValidationOutcome
from app.schemas.market_data import Instrument, MarketReference, Tick

_NOW = datetime(2026, 9, 11, 4, 0, tzinfo=UTC)
_T0 = datetime(2026, 9, 11, 3, 30, tzinfo=UTC)  # 30 min before now -> not future


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _tick(symbol: str = "RELIANCE", *, offset: int = 0, price: str = "100") -> Tick:
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=_T0 + timedelta(seconds=offset),
        last_price=Decimal(price),
        traded_quantity=5,
    )


def _tick_at(when: datetime, symbol: str = "RELIANCE", *, price: str = "100") -> Tick:
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=when,
        last_price=Decimal(price),
        traded_quantity=5,
    )


def _engine(symbols: tuple[str, ...] = ("RELIANCE", "TCS")) -> tuple[TickEngine, list[Event]]:
    registry = InstrumentStateRegistry(_instrument(symbol) for symbol in symbols)
    bus = EventBus()
    recorded: list[Event] = []
    bus.subscribe(MarketContextCreated, recorded.append)
    bus.subscribe(MarketContextUpdated, recorded.append)
    engine = TickEngine(
        registry=registry, bus=bus, clock=ManualClock(_NOW), sequence=MonotonicSequence()
    )
    return engine, recorded


def test_accept_counts_only_acceptance() -> None:
    engine, _ = _engine()
    result = engine.process(_tick())
    assert result.outcome is ValidationOutcome.ACCEPT
    snap = engine.diagnostics_snapshot()
    assert snap.ticks_received == 1
    assert snap.ticks_accepted == 1
    assert snap.ticks_rejected_total == 0
    assert snap.rejected_by_reason == {"invalid": 0, "duplicate": 0, "stale": 0}
    assert snap.last_accepted_event_timestamp == _T0
    assert snap.last_rejected_reason is None


def test_invalid_future_is_counted_and_reason_recorded() -> None:
    engine, recorded = _engine()
    future = _tick_at(_NOW + timedelta(minutes=2))  # beyond the 1-minute skew
    result = engine.process(future)
    assert result.outcome is ValidationOutcome.INVALID
    assert result.context is None
    assert recorded == []  # rejection publishes nothing
    snap = engine.diagnostics_snapshot()
    assert snap.ticks_received == 1
    assert snap.ticks_accepted == 0
    assert snap.ticks_rejected_total == 1
    assert snap.rejected_by_reason["invalid"] == 1
    assert snap.last_rejected_reason == "invalid"
    assert snap.last_rejected_event_timestamp == future.event_timestamp
    assert snap.last_rejection_observed_at == _NOW
    assert snap.last_rejected_event_clock_delta_seconds == 120.0


def test_plus_five_thirty_delta_is_revealed_without_correction() -> None:
    engine, _ = _engine()
    ist_skew = _tick_at(_NOW + timedelta(hours=5, minutes=30))
    result = engine.process(ist_skew)
    # The engine still rejects as INVALID (future) — diagnostics change nothing.
    assert result.outcome is ValidationOutcome.INVALID
    snap = engine.diagnostics_snapshot()
    assert snap.last_rejected_reason == "invalid"
    assert snap.last_rejected_event_clock_delta_seconds == 19800.0  # ~ +05:30, unmodified


def test_duplicate_is_counted_once() -> None:
    engine, _ = _engine()
    tick = _tick(offset=0)
    assert engine.process(tick).outcome is ValidationOutcome.ACCEPT
    assert engine.process(tick).outcome is ValidationOutcome.DUPLICATE
    snap = engine.diagnostics_snapshot()
    assert snap.ticks_received == 2
    assert snap.ticks_accepted == 1
    assert snap.rejected_by_reason["duplicate"] == 1
    assert snap.last_rejected_reason == "duplicate"


def test_stale_is_counted() -> None:
    engine, _ = _engine()
    assert engine.process(_tick(offset=5)).outcome is ValidationOutcome.ACCEPT
    assert engine.process(_tick(offset=1)).outcome is ValidationOutcome.STALE
    snap = engine.diagnostics_snapshot()
    assert snap.rejected_by_reason["stale"] == 1
    assert snap.last_rejected_reason == "stale"


def test_unknown_instrument_counts_invalid() -> None:
    engine, _ = _engine()
    result = engine.process(_tick(symbol="UNLISTED"))
    assert result.outcome is ValidationOutcome.INVALID
    snap = engine.diagnostics_snapshot()
    assert snap.rejected_by_reason["invalid"] == 1


def test_counter_invariant_over_mixed_stream() -> None:
    engine, _ = _engine()
    engine.process(_tick(offset=5))  # accept
    engine.process(_tick(offset=5))  # duplicate
    engine.process(_tick(offset=1))  # stale
    engine.process(_tick_at(_NOW + timedelta(minutes=2)))  # invalid (future)
    engine.process(_tick(symbol="UNLISTED"))  # invalid (unknown)
    snap = engine.diagnostics_snapshot()
    assert snap.ticks_received == snap.ticks_accepted + snap.ticks_rejected_total
    assert snap.ticks_rejected_total == sum(snap.rejected_by_reason.values())


def test_reference_events_counted_separately_from_ticks() -> None:
    engine, _ = _engine()
    engine.process(_tick(offset=5))  # a tick
    engine.process(MarketReference(instrument=_instrument(), previous_close=Decimal("99")))
    engine.process(MarketReference(instrument=_instrument("UNLISTED"), previous_close=Decimal("1")))
    snap = engine.diagnostics_snapshot()
    assert snap.ticks_received == 1  # references never inflate the tick invariant
    assert snap.references_received == 2
    assert snap.references_accepted == 1
    assert snap.references_rejected == 1


def test_behavior_parity_outcomes_and_events_unchanged() -> None:
    engine, recorded = _engine()
    outcomes = [
        engine.process(_tick(offset=0)).outcome,  # created
        engine.process(_tick(offset=1, price="101")).outcome,  # updated
        engine.process(_tick(offset=1, price="101")).outcome,  # duplicate
        engine.process(_tick(offset=0)).outcome,  # stale
    ]
    assert outcomes == [
        ValidationOutcome.ACCEPT,
        ValidationOutcome.ACCEPT,
        ValidationOutcome.DUPLICATE,
        ValidationOutcome.STALE,
    ]
    # Only the two accepts publish; diagnostics are a pure side channel.
    assert [type(event) for event in recorded] == [MarketContextCreated, MarketContextUpdated]


def test_diagnostics_memory_is_bounded_over_large_stream() -> None:
    # Clock far ahead of every event so each successive tick is accepted (past, monotonic).
    registry = InstrumentStateRegistry([_instrument()])
    engine = TickEngine(
        registry=registry,
        bus=EventBus(),
        clock=ManualClock(_T0 + timedelta(days=1)),
        sequence=MonotonicSequence(),
    )
    for offset in range(5000):
        engine.process(_tick(offset=offset, price=str(100 + offset)))
    snap = engine.diagnostics_snapshot()
    assert snap.ticks_accepted == 5000
    # No per-tick / per-instrument growth: reason table stays at the fixed 3 keys.
    assert set(snap.rejected_by_reason) == {"invalid", "duplicate", "stale"}
