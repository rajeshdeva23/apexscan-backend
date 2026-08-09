"""Interleaving (isolation/ordering) and replay determinism for the tick engine.

The engine is synchronous, so independent-instrument processing is proven by
deterministic interleaving rather than threads or sleeps (docs/06 §12.8, §19.4).
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
from app.schemas.market_data import Instrument, Tick

_NOW = datetime(2026, 8, 6, 7, 0, tzinfo=UTC)
_T0 = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)
_SYMBOLS = ("RELIANCE", "TCS")


def _tick(symbol: str, offset: int) -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        event_timestamp=_T0 + timedelta(seconds=offset),
        last_price=Decimal("100") + Decimal(offset),
        traded_quantity=1,
    )


def _engine() -> tuple[TickEngine, list[Event]]:
    registry = InstrumentStateRegistry(Instrument(exchange="NSE", symbol=s) for s in _SYMBOLS)
    bus = EventBus()
    recorded: list[Event] = []
    bus.subscribe(MarketContextCreated, recorded.append)
    bus.subscribe(MarketContextUpdated, recorded.append)
    engine = TickEngine(
        registry=registry, bus=bus, clock=ManualClock(_NOW), sequence=MonotonicSequence()
    )
    return engine, recorded


# A deterministic interleaved stream across two independent instruments.
_STREAM = (
    ("RELIANCE", 0),
    ("TCS", 0),
    ("RELIANCE", 1),
    ("TCS", 1),
    ("RELIANCE", 2),
)


def test_interleaved_instruments_keep_independent_version_chains() -> None:
    engine, _ = _engine()
    versions: dict[str, list[int]] = {"RELIANCE": [], "TCS": []}
    for symbol, offset in _STREAM:
        result = engine.process(_tick(symbol, offset))
        assert result.context is not None
        versions[symbol].append(result.context.version)
    assert versions["RELIANCE"] == [1, 2, 3]
    assert versions["TCS"] == [1, 2]


def test_interleaving_does_not_contaminate_the_other_instrument() -> None:
    engine, _ = _engine()
    for symbol, offset in _STREAM:
        engine.process(_tick(symbol, offset))
    final_tcs = engine.process(_tick("TCS", 2)).context
    assert final_tcs is not None
    assert final_tcs.instrument.symbol == "TCS"
    assert str(final_tcs.latest_tick.last_price) == "102"  # type: ignore[union-attr]


def test_duplicate_under_interleave_only_affects_its_own_instrument() -> None:
    engine, _ = _engine()
    engine.process(_tick("RELIANCE", 0))
    engine.process(_tick("TCS", 0))
    duplicate = engine.process(_tick("RELIANCE", 0))  # exact repeat -> ignored
    advanced_tcs = engine.process(_tick("TCS", 1))
    assert duplicate.context is None
    assert advanced_tcs.context is not None
    assert advanced_tcs.context.version == 2


def _replay() -> list[tuple[str, int, int]]:
    engine, recorded = _engine()
    for symbol, offset in _STREAM:
        engine.process(_tick(symbol, offset))
    return [
        (event.context.instrument.symbol, event.context.version, event.context.sequence)  # type: ignore[attr-defined]
        for event in recorded
    ]


def test_replaying_the_same_stream_produces_identical_output() -> None:
    assert _replay() == _replay()


def test_replay_sequence_is_globally_monotonic_across_instruments() -> None:
    sequences = [sequence for _, _, sequence in _replay()]
    assert sequences == [1, 2, 3, 4, 5]
