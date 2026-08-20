"""TickEngine ↔ MarketContext session-statistics integration (P4.6C; ADR-008).

Proves the accepted-datum path stamps session statistics into the same single
MarketContext version, the default authority never produces AUTHORITATIVE statistics
(P4.6D still pending), phase/reset/progression policy from P4.6B survives wiring, and
one accepted datum yields exactly one version and one lifecycle event.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from app.events.bus import Event, EventBus
from app.market_engine.clock import ManualClock
from app.market_engine.context import MarketContext, SessionStatisticsQuality
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.session import MarketSessionClassifier, SessionSchedule, TradingCalendar
from app.market_engine.session_statistics import SessionStatisticsAuthority
from app.market_engine.state import InstrumentState, InstrumentStateRegistry
from app.market_engine.tick_engine import TickEngine
from app.schemas.market_data import Instrument, ProviderSessionOhlc, Quote, Tick

_NOW = datetime(2026, 8, 7, 13, 0, tzinfo=UTC)  # clock after every event (no future-skew reject)
_LIVE = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)  # 12:00 IST — live session
_LIVE2 = _LIVE + timedelta(minutes=1)
_LIVE3 = _LIVE + timedelta(minutes=2)
_PRE_OPEN = datetime(2026, 8, 6, 3, 32, tzinfo=UTC)  # 09:02 IST
_AUCTION = datetime(2026, 8, 6, 3, 40, tzinfo=UTC)  # 09:10 IST
_CLOSING = datetime(2026, 8, 6, 10, 5, tzinfo=UTC)  # 15:35 IST
_CLOSED = datetime(2026, 8, 6, 11, 0, tzinfo=UTC)  # 16:30 IST
_LIVE_D2 = datetime(2026, 8, 7, 6, 30, tzinfo=UTC)  # next trading day
_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)
_SYMBOLS = ("RELIANCE", "TCS")
_VERIFIED = SessionStatisticsAuthority(tick_aggregate_verified=True)  # ticks carry the aggregate
_UNVERIFIED = SessionStatisticsAuthority()


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _tick(
    symbol: str = "RELIANCE",
    *,
    at: datetime = _LIVE,
    price: str = "100",
    open_: str = "100",
    high: str = "105",
    low: str = "98",
    close: str = "101",
    with_ohlc: bool = True,
) -> Tick:
    ohlc = (
        ProviderSessionOhlc(
            open_price=Decimal(open_),
            high_price=Decimal(high),
            low_price=Decimal(low),
            close_price=Decimal(close),
        )
        if with_ohlc
        else None
    )
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=at,
        last_price=Decimal(price),
        traded_quantity=1,
        session_ohlc=ohlc,
    )


def _quote(symbol: str = "RELIANCE", *, at: datetime = _LIVE2) -> Quote:
    return Quote(
        instrument=_instrument(symbol),
        event_timestamp=at,
        bid_price=Decimal("100"),
        ask_price=Decimal("100.5"),
        bid_quantity=1,
        ask_quantity=1,
    )


def _classifier(*, holidays: tuple[date, ...] = ()) -> MarketSessionClassifier:
    return MarketSessionClassifier(
        schedule=_SCHEDULE,
        calendar=TradingCalendar(holidays=holidays),
        exchange_timezone="Asia/Kolkata",
    )


def _engine(
    *,
    authority: SessionStatisticsAuthority = _UNVERIFIED,
    classifier: MarketSessionClassifier | None = None,
) -> tuple[TickEngine, list[Event]]:
    registry = InstrumentStateRegistry(_instrument(symbol) for symbol in _SYMBOLS)
    bus = EventBus()
    recorded: list[Event] = []
    bus.subscribe(MarketContextCreated, recorded.append)
    bus.subscribe(MarketContextUpdated, recorded.append)
    engine = TickEngine(
        registry=registry,
        bus=bus,
        clock=ManualClock(_NOW),
        sequence=MonotonicSequence(),
        session=classifier if classifier is not None else _classifier(),
        session_statistics_authority=authority,
    )
    return engine, recorded


def _state(engine: TickEngine, symbol: str = "RELIANCE") -> InstrumentState:
    state = engine._registry.get(_instrument(symbol))  # noqa: SLF001 (white-box engine test)
    assert state is not None
    return state


# --------------------------------------------------------------------------- #
# Field + authority gate
# --------------------------------------------------------------------------- #
def test_marketcontext_field_defaults_to_none() -> None:
    context = MarketContext.initial(
        _instrument(), sequence=1, event_timestamp=_LIVE, observed_at=_NOW
    )
    assert context.session_statistics is None


def test_default_authority_never_produces_authoritative_statistics() -> None:
    engine, _ = _engine(authority=_UNVERIFIED)
    result = engine.process(_tick(at=_LIVE))
    assert result.context is not None
    assert result.context.session_statistics is None  # the P4.6C safety invariant


def test_verified_authority_stamps_authoritative_statistics_same_version() -> None:
    engine, recorded = _engine(authority=_VERIFIED)
    result = engine.process(_tick(at=_LIVE, open_="100", high="105", low="98"))
    context = result.context
    assert context is not None and context.session_statistics is not None
    stats = context.session_statistics
    assert stats.quality is SessionStatisticsQuality.AUTHORITATIVE
    assert (stats.open_price, stats.high_price, stats.low_price) == (
        Decimal("100"),
        Decimal("105"),
        Decimal("98"),
    )
    assert stats.trading_date == date(2026, 8, 6)
    assert stats.as_of == _LIVE
    assert context.version == 1
    assert [type(event) for event in recorded] == [MarketContextCreated]


def test_state_value_equals_stamped_context_value() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    result = engine.process(_tick(at=_LIVE))
    assert result.context is not None
    assert _state(engine).session_statistics == result.context.session_statistics


def test_first_mid_session_packet_immediately_stamps_statistics() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    result = engine.process(_tick(at=_LIVE, high="105", low="98"))
    assert result.context is not None and result.context.session_statistics is not None
    assert result.context.session_statistics.high_price == Decimal("105")


# --------------------------------------------------------------------------- #
# Missing / same / progression / fail-closed
# --------------------------------------------------------------------------- #
def test_missing_aggregate_with_no_prior_is_none() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    result = engine.process(_tick(at=_LIVE, with_ohlc=False))
    assert result.context is not None and result.context.session_statistics is None


def test_missing_aggregate_retains_prior() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    established = engine.process(_tick(at=_LIVE)).context
    result = engine.process(_tick(at=_LIVE2, with_ohlc=False))
    assert result.context is not None
    assert result.context.session_statistics == established.session_statistics  # type: ignore[union-attr]


def test_same_aggregate_retains_statistics() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    first = engine.process(_tick(at=_LIVE, high="105", low="98")).context
    second = engine.process(_tick(at=_LIVE2, high="105", low="98")).context
    assert second is not None and second.session_statistics == first.session_statistics  # type: ignore[union-attr]


def test_high_progression_is_reflected() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    engine.process(_tick(at=_LIVE, high="105", low="98"))
    result = engine.process(_tick(at=_LIVE2, high="108", low="98"))
    assert result.context is not None and result.context.session_statistics is not None
    assert result.context.session_statistics.high_price == Decimal("108")


def test_low_progression_is_reflected() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    engine.process(_tick(at=_LIVE, high="105", low="98"))
    result = engine.process(_tick(at=_LIVE2, high="105", low="95"))
    assert result.context is not None and result.context.session_statistics is not None
    assert result.context.session_statistics.low_price == Decimal("95")


def test_high_regression_is_retained() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    engine.process(_tick(at=_LIVE, high="108", low="95"))
    result = engine.process(_tick(at=_LIVE2, high="106", low="95"))
    assert result.context is not None and result.context.session_statistics is not None
    assert result.context.session_statistics.high_price == Decimal("108")


def test_low_regression_is_retained() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    engine.process(_tick(at=_LIVE, high="108", low="95"))
    result = engine.process(_tick(at=_LIVE2, high="108", low="97"))
    assert result.context is not None and result.context.session_statistics is not None
    assert result.context.session_statistics.low_price == Decimal("95")


def test_open_change_is_retained() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    engine.process(_tick(at=_LIVE, open_="100", high="105", low="98"))
    result = engine.process(_tick(at=_LIVE2, open_="101", high="108", low="97"))
    assert result.context is not None and result.context.session_statistics is not None
    assert result.context.session_statistics.open_price == Decimal("100")


# --------------------------------------------------------------------------- #
# Phase behavior
# --------------------------------------------------------------------------- #
def test_pre_open_cannot_establish_statistics() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    result = engine.process(_tick(at=_PRE_OPEN))
    assert result.context is not None and result.context.session_statistics is None


def test_opening_auction_cannot_establish_statistics() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    result = engine.process(_tick(at=_AUCTION))
    assert result.context is not None and result.context.session_statistics is None


def test_closing_session_does_not_progress() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    engine.process(_tick(at=_LIVE, high="105", low="98"))
    result = engine.process(_tick(at=_CLOSING, high="120", low="90"))
    assert result.context is not None and result.context.session_statistics is not None
    assert result.context.session_statistics.high_price == Decimal("105")


def test_market_closed_does_not_progress() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    engine.process(_tick(at=_LIVE, high="105", low="98"))
    result = engine.process(_tick(at=_CLOSED, high="130", low="80"))
    assert result.context is not None and result.context.session_statistics is not None
    assert result.context.session_statistics.high_price == Decimal("105")


def test_emergency_halt_does_not_progress() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    engine.process(_tick(at=_LIVE, high="105", low="98"))
    engine.set_halt(active=True)
    result = engine.process(_tick(at=_LIVE2, high="200", low="50"))
    assert result.context is not None and result.context.session_statistics is not None
    assert result.context.session_statistics.high_price == Decimal("105")
    assert result.context.session_statistics.as_of == _LIVE  # as_of not advanced


def test_holiday_cannot_establish_statistics() -> None:
    engine, _ = _engine(authority=_VERIFIED, classifier=_classifier(holidays=(date(2026, 8, 6),)))
    result = engine.process(_tick(at=_LIVE))
    assert result.context is not None and result.context.session_statistics is None


# --------------------------------------------------------------------------- #
# Trading-date reset
# --------------------------------------------------------------------------- #
def test_trading_date_change_resets_and_does_not_leak() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    day1 = engine.process(_tick(at=_LIVE, open_="100", high="105", low="98")).context
    day2 = engine.process(_tick(at=_LIVE_D2, open_="200", high="205", low="198", close="200"))
    assert day2.context is not None and day2.context.session_statistics is not None
    stats = day2.context.session_statistics
    assert stats.trading_date == date(2026, 8, 7)
    assert (stats.open_price, stats.high_price, stats.low_price) == (
        Decimal("200"),
        Decimal("205"),
        Decimal("198"),
    )
    assert day1 is not None and day1.session_statistics is not None
    assert day1.session_statistics.high_price == Decimal("105")  # prior context immutable


def test_new_day_before_valid_aggregate_is_none() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    engine.process(_tick(at=_LIVE, high="105", low="98"))
    result = engine.process(_tick(at=_LIVE_D2, with_ohlc=False))
    assert result.context is not None and result.context.session_statistics is None


# --------------------------------------------------------------------------- #
# One-datum/one-version, rejection atomicity, immutability, isolation, replay
# --------------------------------------------------------------------------- #
def test_one_accepted_datum_yields_one_version_and_one_event() -> None:
    engine, recorded = _engine(authority=_VERIFIED)
    engine.process(_tick(at=_LIVE))
    second = engine.process(_tick(at=_LIVE2, high="108"))
    assert second.context is not None and second.context.version == 2
    assert [type(event) for event in recorded] == [MarketContextCreated, MarketContextUpdated]


def test_only_marketcontext_events_are_published() -> None:
    engine, recorded = _engine(authority=_VERIFIED)
    engine.process(_tick(at=_LIVE))
    engine.process(_tick(at=_LIVE2, high="108"))
    assert all(isinstance(event, MarketContextCreated | MarketContextUpdated) for event in recorded)


def test_rejected_stale_tick_does_not_mutate_statistics_or_version() -> None:
    engine, recorded = _engine(authority=_VERIFIED)
    engine.process(_tick(at=_LIVE2, high="108", low="95"))
    before_state = _state(engine).session_statistics
    before_version = _state(engine).context.version  # type: ignore[union-attr]
    events_before = len(recorded)
    stale = engine.process(_tick(at=_LIVE, high="999", low="1"))  # earlier → rejected
    assert stale.context is None
    assert _state(engine).session_statistics == before_state
    assert _state(engine).context.version == before_version  # type: ignore[union-attr]
    assert len(recorded) == events_before


def test_quote_update_preserves_prior_statistics() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    established = engine.process(_tick(at=_LIVE, high="105", low="98")).context
    result = engine.process(_quote(at=_LIVE2))
    assert result.context is not None
    assert result.context.session_statistics == established.session_statistics  # type: ignore[union-attr]


def test_updates_are_isolated_per_instrument() -> None:
    engine, _ = _engine(authority=_VERIFIED)
    engine.process(_tick("RELIANCE", at=_LIVE, high="105", low="98"))
    b_context = engine.process(
        _tick("TCS", at=_LIVE, open_="200", high="210", low="195", close="200")
    ).context
    engine.process(_tick("RELIANCE", at=_LIVE2, high="108"))
    assert _state(engine, "TCS").session_statistics == b_context.session_statistics  # type: ignore[union-attr]
    assert _state(engine, "TCS").session_statistics.high_price == Decimal("210")  # type: ignore[union-attr]


def test_replay_is_deterministic() -> None:
    def run() -> list[object]:
        engine, _ = _engine(authority=_VERIFIED)
        ticks = (
            _tick(at=_LIVE, high="105", low="98"),
            _tick(at=_LIVE2, high="108", low="97"),
            _tick(at=_LIVE3, high="108", low="97"),
        )
        return [engine.process(tick).context.session_statistics for tick in ticks]  # type: ignore[union-attr]

    assert run() == run()
