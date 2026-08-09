"""Historical carry-forward, isolation, and determinism in the engine (P4.5A; §33)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.events.bus import Event, EventBus
from app.market_engine.clock import ManualClock
from app.market_engine.context import MarketContext
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.historical.context import HistoricalContext, HistoricalSeries
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.tick_engine import TickEngine
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, Instrument, Quote, Tick

_NOW = datetime(2026, 8, 6, 7, 0, tzinfo=UTC)
_T0 = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)
_FIVE = Timeframe.minutes(5)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _tick(symbol: str = "RELIANCE", *, offset: int = 0) -> Tick:
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=_T0 + timedelta(seconds=offset),
        last_price=Decimal("100"),
        traded_quantity=5,
    )


def _quote(symbol: str = "RELIANCE", *, offset: int = 0) -> Quote:
    return Quote(
        instrument=_instrument(symbol),
        event_timestamp=_T0 + timedelta(seconds=offset),
        bid_price=Decimal("99"),
        ask_price=Decimal("101"),
        bid_quantity=1,
        ask_quantity=1,
    )


def _candle(symbol: str = "RELIANCE") -> Candle:
    return Candle(
        instrument=_instrument(symbol),
        start_timestamp=datetime(2026, 8, 5, 3, 45, tzinfo=UTC),
        end_timestamp=datetime(2026, 8, 5, 3, 50, tzinfo=UTC),
        open_price=Decimal("100"),
        high_price=Decimal("101"),
        low_price=Decimal("99"),
        close_price=Decimal("100"),
        traded_quantity=10,
    )


def _history(symbol: str = "RELIANCE") -> HistoricalContext:
    series = HistoricalSeries(timeframe=_FIVE, candles=(_candle(symbol),))
    return HistoricalContext(instrument=_instrument(symbol), series=(series,))


def _engine(
    symbols: tuple[str, ...] = ("RELIANCE", "TCS"),
) -> tuple[TickEngine, InstrumentStateRegistry, list[Event]]:
    registry = InstrumentStateRegistry(_instrument(symbol) for symbol in symbols)
    bus = EventBus()
    recorded: list[Event] = []
    bus.subscribe(MarketContextCreated, recorded.append)
    bus.subscribe(MarketContextUpdated, recorded.append)
    engine = TickEngine(
        registry=registry, bus=bus, clock=ManualClock(_NOW), sequence=MonotonicSequence()
    )
    return engine, registry, recorded


def test_historical_is_optional_and_defaults_to_none() -> None:
    engine, _, _ = _engine()
    result = engine.process(_tick())
    assert result.context is not None
    assert result.context.historical is None


def test_installed_history_surfaces_on_the_next_accepted_datum() -> None:
    engine, registry, _ = _engine()
    registry.install_historical(_instrument(), _history())
    result = engine.process(_tick())
    assert result.context is not None
    assert result.context.historical is not None
    assert result.context.historical.series[0].timeframe == _FIVE


def test_history_survives_a_subsequent_tick() -> None:
    engine, registry, _ = _engine()
    registry.install_historical(_instrument(), _history())
    engine.process(_tick(offset=0))
    result = engine.process(_tick(offset=1))
    assert result.context is not None
    assert result.context.historical is not None


def test_history_survives_a_quote() -> None:
    engine, registry, _ = _engine()
    registry.install_historical(_instrument(), _history())
    engine.process(_tick(offset=0))
    result = engine.process(_quote(offset=1))
    assert result.context is not None
    assert result.context.historical is not None


def test_install_alone_creates_no_version_and_no_event() -> None:
    engine, registry, recorded = _engine()
    registry.install_historical(_instrument(), _history())
    state = registry.get(_instrument())
    assert state is not None
    assert state.context is None  # no MarketContext version minted by installation
    assert recorded == []  # no event published by installation


def test_rejected_datum_creates_no_history_only_version() -> None:
    engine, registry, recorded = _engine()
    registry.install_historical(_instrument(), _history())
    # Unknown instrument is rejected: no acceptance, so history never surfaces.
    result = engine.process(_tick("UNKNOWN"))
    assert result.context is None
    assert recorded == []


def test_replacement_surfaces_only_on_a_later_accepted_datum() -> None:
    engine, registry, _ = _engine()
    registry.install_historical(_instrument(), _history())
    first = engine.process(_tick(offset=0))
    replacement = _history()
    registry.install_historical(_instrument(), replacement)
    assert first.context is not None
    assert first.context.historical is not replacement  # earlier version unchanged
    second = engine.process(_tick(offset=1))
    assert second.context is not None
    assert second.context.historical is replacement


def test_per_instrument_isolation() -> None:
    engine, registry, _ = _engine()
    registry.install_historical(_instrument("RELIANCE"), _history("RELIANCE"))
    registry.install_historical(_instrument("TCS"), _history("TCS"))
    reliance = engine.process(_tick("RELIANCE"))
    tcs = engine.process(_tick("TCS"))
    assert reliance.context is not None
    assert tcs.context is not None
    assert reliance.context.historical is not None
    assert tcs.context.historical is not None
    assert reliance.context.historical.instrument == _instrument("RELIANCE")
    assert tcs.context.historical.instrument == _instrument("TCS")


def test_replacing_one_instrument_does_not_affect_another() -> None:
    engine, registry, _ = _engine()
    registry.install_historical(_instrument("RELIANCE"), _history("RELIANCE"))
    tcs_history = _history("TCS")
    registry.install_historical(_instrument("TCS"), tcs_history)
    engine.process(_tick("RELIANCE"))
    registry.install_historical(_instrument("RELIANCE"), _history("RELIANCE"))
    tcs = engine.process(_tick("TCS"))
    assert tcs.context is not None
    assert tcs.context.historical is tcs_history


def test_same_snapshot_is_reused_by_reference_not_rebuilt() -> None:
    engine, registry, _ = _engine()
    history = _history()
    registry.install_historical(_instrument(), history)
    first = engine.process(_tick(offset=0))
    second = engine.process(_tick(offset=1))
    third = engine.process(_quote(offset=2))
    assert first.context is not None
    assert second.context is not None
    assert third.context is not None
    assert first.context.historical is history
    assert second.context.historical is history
    assert third.context.historical is history


def _replay() -> list[tuple[int, bool]]:
    engine, registry, recorded = _engine(("RELIANCE",))
    registry.install_historical(_instrument(), _history())
    for offset in (0, 1, 2):
        engine.process(_tick(offset=offset))
    log: list[tuple[int, bool]] = []
    for event in recorded:
        context: MarketContext = event.context  # type: ignore[attr-defined]
        log.append((context.version, context.historical is not None))
    return log


def test_history_carry_forward_is_replay_deterministic() -> None:
    assert _replay() == _replay()
