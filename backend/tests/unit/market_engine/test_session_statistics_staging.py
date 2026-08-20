"""Staging and next-datum surfacing of SessionStatisticsObservation (P4.6E2; ADR-009)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.events.bus import Event, EventBus
from app.market_engine.clock import ManualClock
from app.market_engine.context import SessionStatisticsQuality
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.historical.context import HistoricalContext
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.session import MarketSessionClassifier, SessionSchedule, TradingCalendar
from app.market_engine.session_statistics import (
    SessionStatisticsAuthority,
    resolve_session_statistics,
)
from app.market_engine.state import InstrumentState, InstrumentStateRegistry
from app.market_engine.tick_engine import TickEngine
from app.schemas.market_data import (
    Instrument,
    ProviderSessionOhlc,
    Quote,
    SessionStatisticsObservation,
    Tick,
)

_NOW = datetime(2026, 8, 7, 13, 0, tzinfo=UTC)  # clock after every event
_LIVE = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)  # 12:00 IST
_LIVE2 = _LIVE + timedelta(minutes=1)
_LIVE3 = _LIVE + timedelta(minutes=2)
_PRE_OPEN = datetime(2026, 8, 6, 3, 32, tzinfo=UTC)  # 09:02 IST
_LIVE_D2 = datetime(2026, 8, 7, 6, 30, tzinfo=UTC)
_DATE = date(2026, 8, 6)
_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)
_VERIFIED = SessionStatisticsAuthority(staged_observation_verified=True)  # observation-driven path
_UNVERIFIED = SessionStatisticsAuthority()


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _ohlc(
    *, open_: str = "100", high: str = "105", low: str = "98", close: str = "101"
) -> ProviderSessionOhlc:
    return ProviderSessionOhlc(
        open_price=Decimal(open_),
        high_price=Decimal(high),
        low_price=Decimal(low),
        close_price=Decimal(close),
    )


def _obs(
    *,
    symbol: str = "RELIANCE",
    trading_date: date = _DATE,
    observed_at: datetime = _LIVE,
    open_: str = "100",
    high: str = "105",
    low: str = "98",
    close: str = "101",
) -> SessionStatisticsObservation:
    return SessionStatisticsObservation(
        instrument=_instrument(symbol),
        trading_date=trading_date,
        observed_at=observed_at,
        session_ohlc=_ohlc(open_=open_, high=high, low=low, close=close),
    )


def _tick(symbol: str = "RELIANCE", *, at: datetime = _LIVE, with_ohlc: bool = False) -> Tick:
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=at,
        last_price=Decimal("100"),
        traded_quantity=1,
        session_ohlc=_ohlc() if with_ohlc else None,
    )


def _quote(symbol: str = "RELIANCE", *, at: datetime = _LIVE) -> Quote:
    return Quote(
        instrument=_instrument(symbol),
        event_timestamp=at,
        bid_price=Decimal("100"),
        ask_price=Decimal("100.5"),
        bid_quantity=1,
        ask_quantity=1,
    )


def _classifier() -> MarketSessionClassifier:
    return MarketSessionClassifier(
        schedule=_SCHEDULE, calendar=TradingCalendar(), exchange_timezone="Asia/Kolkata"
    )


def _registry(*symbols: str) -> InstrumentStateRegistry:
    return InstrumentStateRegistry(_instrument(s) for s in (symbols or ("RELIANCE",)))


def _engine(
    *, authority: SessionStatisticsAuthority = _VERIFIED, symbols: tuple[str, ...] = ("RELIANCE",)
) -> tuple[TickEngine, InstrumentStateRegistry, list[Event]]:
    registry = _registry(*symbols)
    bus = EventBus()
    recorded: list[Event] = []
    bus.subscribe(MarketContextCreated, recorded.append)
    bus.subscribe(MarketContextUpdated, recorded.append)
    engine = TickEngine(
        registry=registry,
        bus=bus,
        clock=ManualClock(_NOW),
        sequence=MonotonicSequence(),
        session=_classifier(),
        session_statistics_authority=authority,
    )
    return engine, registry, recorded


def _state(registry: InstrumentStateRegistry, symbol: str = "RELIANCE") -> InstrumentState:
    state = registry.get(_instrument(symbol))
    assert state is not None
    return state


# --------------------------------------------------------------------------- #
# Registry staging
# --------------------------------------------------------------------------- #
def test_staging_creates_no_context_and_no_state_version() -> None:
    registry = _registry()
    registry.stage_session_statistics_observation(_instrument(), _obs())
    state = _state(registry)
    assert state.staged_session_statistics_observation == _obs()
    assert state.context is None  # staging mints no MarketContext version


def test_staging_instrument_mismatch_is_rejected() -> None:
    registry = _registry("RELIANCE", "TCS")
    with pytest.raises(ValueError, match="must match"):
        registry.stage_session_statistics_observation(_instrument("TCS"), _obs(symbol="RELIANCE"))


def test_newer_observation_replaces_older() -> None:
    registry = _registry()
    registry.stage_session_statistics_observation(_instrument(), _obs(observed_at=_LIVE))
    registry.stage_session_statistics_observation(
        _instrument(), _obs(observed_at=_LIVE2, high="108")
    )
    assert _state(registry).staged_session_statistics_observation.observed_at == _LIVE2  # type: ignore[union-attr]


def test_older_observation_is_ignored() -> None:
    registry = _registry()
    registry.stage_session_statistics_observation(_instrument(), _obs(observed_at=_LIVE2))
    registry.stage_session_statistics_observation(
        _instrument(), _obs(observed_at=_LIVE, high="106")
    )
    assert _state(registry).staged_session_statistics_observation.observed_at == _LIVE2  # type: ignore[union-attr]


def test_same_instant_identical_observation_is_idempotent() -> None:
    registry = _registry()
    registry.stage_session_statistics_observation(_instrument(), _obs(observed_at=_LIVE))
    registry.stage_session_statistics_observation(_instrument(), _obs(observed_at=_LIVE))
    assert _state(registry).staged_session_statistics_observation == _obs(observed_at=_LIVE)


def test_same_instant_conflicting_observation_is_rejected() -> None:
    registry = _registry()
    registry.stage_session_statistics_observation(
        _instrument(), _obs(observed_at=_LIVE, high="105")
    )
    with pytest.raises(ValueError, match="conflicting"):
        registry.stage_session_statistics_observation(
            _instrument(), _obs(observed_at=_LIVE, high="108")
        )


def test_staging_is_bounded_to_one_observation() -> None:
    registry = _registry()
    for minute in range(5):
        registry.stage_session_statistics_observation(
            _instrument(), _obs(observed_at=_LIVE + timedelta(minutes=minute))
        )
    # A single staged slot regardless of how many were staged.
    assert _state(registry).staged_session_statistics_observation is not None


# --------------------------------------------------------------------------- #
# Pure resolve precedence / eligibility
# --------------------------------------------------------------------------- #
def test_resolve_applies_eligible_observation_and_consumes_it() -> None:
    stats, staged = resolve_session_statistics(
        aggregate=None,
        aggregate_as_of=_LIVE,
        staged=_obs(observed_at=_LIVE, high="108"),
        session=_classifier().classify(_LIVE),
        previous=None,
        authority=_VERIFIED,
    )
    assert staged is None  # consumed
    assert stats is not None and stats.high_price == Decimal("108")


def test_resolve_drops_stale_prior_day_observation() -> None:
    _stats, staged = resolve_session_statistics(
        aggregate=None,
        aggregate_as_of=_LIVE_D2,
        staged=_obs(trading_date=_DATE, observed_at=_LIVE),
        session=_classifier().classify(_LIVE_D2),  # next trading day
        previous=None,
        authority=_VERIFIED,
    )
    assert staged is None  # prior-day observation dropped, no leakage


def test_resolve_retains_observation_before_live_session() -> None:
    kept = _obs(observed_at=_LIVE)
    _stats, staged = resolve_session_statistics(
        aggregate=None,
        aggregate_as_of=_PRE_OPEN,
        staged=kept,
        session=_classifier().classify(_PRE_OPEN),  # PRE_OPEN, same date
        previous=None,
        authority=_VERIFIED,
    )
    assert staged == kept  # retained pending until LIVE_SESSION


# --------------------------------------------------------------------------- #
# TickEngine surfacing
# --------------------------------------------------------------------------- #
def test_staged_observation_surfaces_on_next_accepted_tick() -> None:
    engine, registry, recorded = _engine()
    registry.stage_session_statistics_observation(_instrument(), _obs(high="105", low="98"))
    result = engine.process(_tick(at=_LIVE))
    assert result.context is not None and result.context.session_statistics is not None
    stats = result.context.session_statistics
    assert stats.quality is SessionStatisticsQuality.AUTHORITATIVE
    assert stats.high_price == Decimal("105")
    assert [type(e) for e in recorded] == [MarketContextCreated]  # one event, none from staging
    assert _state(registry).staged_session_statistics_observation is None  # consumed


def test_staged_observation_surfaces_on_next_accepted_quote() -> None:
    engine, registry, _ = _engine()
    registry.stage_session_statistics_observation(_instrument(), _obs(high="107"))
    result = engine.process(_quote(at=_LIVE))
    assert result.context is not None and result.context.session_statistics is not None
    assert result.context.session_statistics.high_price == Decimal("107")


def test_one_accepted_datum_one_version_with_staged_observation() -> None:
    engine, registry, recorded = _engine()
    registry.stage_session_statistics_observation(_instrument(), _obs())
    result = engine.process(_tick(at=_LIVE))
    assert result.context is not None and result.context.version == 1
    assert len(recorded) == 1


def test_historical_and_staged_statistics_surface_in_one_version() -> None:
    engine, registry, recorded = _engine()
    registry.install_historical(_instrument(), HistoricalContext(instrument=_instrument()))
    registry.stage_session_statistics_observation(_instrument(), _obs(high="106"))
    result = engine.process(_tick(at=_LIVE))
    assert result.context is not None
    assert result.context.historical is not None
    assert result.context.session_statistics is not None
    assert result.context.session_statistics.high_price == Decimal("106")
    assert [type(e) for e in recorded] == [MarketContextCreated]


def test_rejected_stale_event_does_not_consume_or_version() -> None:
    engine, registry, recorded = _engine()
    engine.process(_tick(at=_LIVE2))  # establishes context v1 (no observation staged yet)
    registry.stage_session_statistics_observation(_instrument(), _obs(observed_at=_LIVE2))
    events_before = len(recorded)
    rejected = engine.process(_tick(at=_LIVE))  # earlier → rejected
    assert rejected.context is None
    assert _state(registry).staged_session_statistics_observation is not None  # not consumed
    assert len(recorded) == events_before  # no version/event


def test_default_authority_does_not_make_staged_observation_authoritative() -> None:
    engine, registry, _ = _engine(authority=_UNVERIFIED)
    registry.stage_session_statistics_observation(_instrument(), _obs())
    result = engine.process(_tick(at=_LIVE))
    assert result.context is not None and result.context.session_statistics is None


def test_verified_authority_establishes_statistics_from_observation() -> None:
    engine, registry, _ = _engine(authority=_VERIFIED)
    registry.stage_session_statistics_observation(
        _instrument(), _obs(open_="100", high="105", low="98")
    )
    result = engine.process(_tick(at=_LIVE))
    assert result.context is not None and result.context.session_statistics is not None
    stats = result.context.session_statistics
    assert (stats.open_price, stats.high_price, stats.low_price) == (
        Decimal("100"),
        Decimal("105"),
        Decimal("98"),
    )
    assert stats.as_of == _LIVE  # the observation's observed_at


def test_progression_via_successive_observations() -> None:
    engine, registry, _ = _engine()
    registry.stage_session_statistics_observation(
        _instrument(), _obs(observed_at=_LIVE, high="105", low="98")
    )
    engine.process(_tick(at=_LIVE))
    registry.stage_session_statistics_observation(
        _instrument(), _obs(observed_at=_LIVE2, high="108", low="95")
    )
    result = engine.process(_tick(at=_LIVE2))
    assert result.context is not None and result.context.session_statistics is not None
    assert result.context.session_statistics.high_price == Decimal("108")
    assert result.context.session_statistics.low_price == Decimal("95")


def test_open_change_observation_is_rejected_and_retains_prior() -> None:
    engine, registry, _ = _engine()
    registry.stage_session_statistics_observation(
        _instrument(), _obs(observed_at=_LIVE, open_="100", high="105", low="98")
    )
    engine.process(_tick(at=_LIVE))
    registry.stage_session_statistics_observation(
        _instrument(), _obs(observed_at=_LIVE2, open_="101", high="108", low="97")
    )
    result = engine.process(_tick(at=_LIVE2))
    assert result.context is not None and result.context.session_statistics is not None
    assert result.context.session_statistics.open_price == Decimal("100")  # unchanged


def test_stale_observation_does_not_regress_statistics() -> None:
    engine, registry, _ = _engine()
    registry.stage_session_statistics_observation(
        _instrument(), _obs(observed_at=_LIVE2, high="108", low="95")
    )
    engine.process(_tick(at=_LIVE2))  # stats as_of=_LIVE2
    registry.stage_session_statistics_observation(
        _instrument(), _obs(observed_at=_LIVE, high="120", low="90")
    )
    result = engine.process(_tick(at=_LIVE3))
    assert result.context is not None and result.context.session_statistics is not None
    assert result.context.session_statistics.high_price == Decimal("108")  # stale ignored


def test_multi_instrument_isolation() -> None:
    engine, registry, _ = _engine(symbols=("RELIANCE", "TCS"))
    registry.stage_session_statistics_observation(
        _instrument("RELIANCE"), _obs(symbol="RELIANCE", high="105")
    )
    registry.stage_session_statistics_observation(
        _instrument("TCS"), _obs(symbol="TCS", open_="200", high="210", low="195", close="200")
    )
    engine.process(_tick("RELIANCE", at=_LIVE))
    # TCS's staged observation is untouched by processing RELIANCE.
    assert _state(registry, "TCS").staged_session_statistics_observation is not None
    assert _state(registry, "TCS").session_statistics is None


def test_prior_context_is_immutable_across_a_new_observation() -> None:
    engine, registry, _ = _engine()
    registry.stage_session_statistics_observation(
        _instrument(), _obs(observed_at=_LIVE, high="105", low="98")
    )
    v1 = engine.process(_tick(at=_LIVE)).context
    registry.stage_session_statistics_observation(
        _instrument(), _obs(observed_at=_LIVE2, high="108", low="98")
    )
    engine.process(_tick(at=_LIVE2))
    assert v1 is not None and v1.session_statistics is not None
    assert v1.session_statistics.high_price == Decimal("105")  # v1 unchanged


def test_replay_is_deterministic() -> None:
    def run() -> list[object]:
        engine, registry, _ = _engine()
        results: list[object] = []
        for at, high in ((_LIVE, "105"), (_LIVE2, "108"), (_LIVE3, "108")):
            registry.stage_session_statistics_observation(
                _instrument(), _obs(observed_at=at, high=high, low="97")
            )
            results.append(engine.process(_tick(at=at)).context.session_statistics)  # type: ignore[union-attr]
        return results

    assert run() == run()
