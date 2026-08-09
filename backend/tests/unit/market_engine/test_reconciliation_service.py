"""Reconciliation orchestration: feed-gap recovery, current-day, race (P4.5D; §28,43,44)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.events.bus import Event, EventBus
from app.market_engine.candle_engine import CandleEngine
from app.market_engine.clock import ManualClock
from app.market_engine.context import MarketState, SessionContext
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.historical.cache import HistoricalCache
from app.market_engine.historical.calendar_window import CalendarCoverage, HistoricalCalendarWindow
from app.market_engine.historical.coordinator import HistoricalCoordinator
from app.market_engine.historical.reconciliation import ReconciliationOutcome
from app.market_engine.historical.requirements import HistoricalRequirement
from app.market_engine.historical.service import HistoricalRangePlanner, HistoricalWarmupService
from app.market_engine.historical.source import HistoricalFetchPlan, interval_for_timeframe
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.session import MarketSessionClassifier, SessionSchedule, TradingCalendar
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.tick_engine import TickEngine
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument, Tick
from tests.fakes.historical_source import Behavior, FakeHistoricalSource

_IST = ZoneInfo("Asia/Kolkata")
_TZ = "Asia/Kolkata"
_PRIOR = date(2026, 8, 6)  # Thursday
_REFERENCE = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)  # Monday
_ONE = Timeframe.minutes(1)
_FIVE = Timeframe.minutes(5)
_SEVEN = Timeframe.minutes(7)
_FIFTEEN = Timeframe.minutes(15)
_SESSION = Timeframe.session()
_DIRECT = frozenset({_ONE, _FIVE, _FIFTEEN, _SESSION})
_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _planner() -> HistoricalRangePlanner:
    window = HistoricalCalendarWindow(
        calendar=TradingCalendar(),
        coverage=CalendarCoverage(start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)),
    )
    return HistoricalRangePlanner(schedule=_SCHEDULE, exchange_timezone=_TZ, calendar_window=window)


def _session(day: date) -> SessionContext:
    return SessionContext(
        trading_date=day, market_state=MarketState.LIVE_SESSION, exchange_timezone=_TZ
    )


def _tick(day: date, hour: int, minute: int, *, symbol: str = "RELIANCE") -> Tick:
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=datetime.combine(day, time(hour, minute), tzinfo=_IST),
        last_price=Decimal("100"),
        traded_quantity=1,
        session_cumulative_volume=100,
    )


def _service(
    source: FakeHistoricalSource,
    engine: CandleEngine,
    instruments: list[Instrument],
    *,
    supports_current_day: bool = False,
) -> HistoricalWarmupService:
    registry = InstrumentStateRegistry(instruments)
    coordinator = HistoricalCoordinator(source=source, cache=HistoricalCache(), max_concurrency=4)
    return HistoricalWarmupService(
        registry=registry,
        coordinator=coordinator,
        planner=_planner(),
        candles=engine,
        supports_current_day=supports_current_day,
    )


def _source(**kwargs: object) -> FakeHistoricalSource:
    return FakeHistoricalSource(direct_timeframes=_DIRECT, **kwargs)  # type: ignore[arg-type]


def _engine(timeframes: list[Timeframe]) -> CandleEngine:
    return CandleEngine(schedule=_SCHEDULE, exchange_timezone=_TZ, timeframes=timeframes)


def _incomplete_of(engine: CandleEngine, instrument: Instrument, timeframe: Timeframe):  # noqa: ANN202
    return next(
        c for c in engine.candle_sets_for(instrument) if c.timeframe == timeframe
    ).incomplete


def _finalized_of(engine: CandleEngine, instrument: Instrument, timeframe: Timeframe):  # noqa: ANN202
    return next(c for c in engine.candle_sets_for(instrument) if c.timeframe == timeframe).finalized


def _outcomes(summary) -> list[ReconciliationOutcome]:  # noqa: ANN001
    return [result.outcome for result in summary.results]


async def test_prior_day_direct_repair_and_idempotent() -> None:
    engine = _engine([_FIVE])
    instrument = _instrument()
    engine.update(_tick(_PRIOR, 9, 16), _session(_PRIOR))
    engine.flush(datetime.combine(_PRIOR, time(9, 20), tzinfo=_IST).astimezone(UTC))
    service = _service(_source(), engine, [instrument])

    summary = await service.reconcile_completed(instrument, reference=_REFERENCE)
    assert ReconciliationOutcome.RECONCILED in _outcomes(summary)
    assert len(_finalized_of(engine, instrument, _FIVE)) == 1
    assert _incomplete_of(engine, instrument, _FIVE) == ()

    again = await service.reconcile_completed(instrument, reference=_REFERENCE)
    assert ReconciliationOutcome.RECONCILED not in _outcomes(again)  # nothing left to repair
    assert len(_finalized_of(engine, instrument, _FIVE)) == 1  # no duplicate


async def test_current_day_is_withheld_with_no_source_call() -> None:
    engine = _engine([_FIVE])
    instrument = _instrument()
    today = _REFERENCE.astimezone(_IST).date()
    engine.update(_tick(today, 9, 16), _session(today))
    engine.flush(datetime.combine(today, time(9, 20), tzinfo=_IST).astimezone(UTC))
    source = _source()
    service = _service(source, engine, [instrument])

    summary = await service.reconcile_completed(instrument, reference=_REFERENCE)
    assert _outcomes(summary) == [ReconciliationOutcome.CURRENT_DAY_WITHHELD]
    assert source.call_count == 0
    assert len(_incomplete_of(engine, instrument, _FIVE)) == 1  # remains


async def test_current_day_repaired_when_capability_enabled() -> None:
    engine = _engine([_FIVE])
    instrument = _instrument()
    today = _REFERENCE.astimezone(_IST).date()
    engine.update(_tick(today, 9, 16), _session(today))
    engine.flush(datetime.combine(today, time(9, 20), tzinfo=_IST).astimezone(UTC))
    service = _service(_source(), engine, [instrument], supports_current_day=True)

    summary = await service.reconcile_completed(instrument, reference=_REFERENCE)
    assert ReconciliationOutcome.RECONCILED in _outcomes(summary)


async def test_multi_bucket_gap_coalesces_to_one_fetch() -> None:
    engine = _engine([_FIVE])
    instrument = _instrument()
    for minute in (16, 21, 26, 31):
        engine.update(_tick(_PRIOR, 9, minute), _session(_PRIOR))
    source = _source()
    service = _service(source, engine, [instrument])

    summary = await service.reconcile_completed(instrument, reference=_REFERENCE)
    assert _outcomes(summary).count(ReconciliationOutcome.RECONCILED) == 3
    assert source.calls_by_interval[timedelta(minutes=5)] == 1  # one covering window


async def test_multi_timeframe_gap_direct_and_reconstructed() -> None:
    engine = _engine([_FIVE, _SEVEN, _FIFTEEN])
    instrument = _instrument()
    engine.update(_tick(_PRIOR, 9, 16), _session(_PRIOR))
    engine.update(_tick(_PRIOR, 9, 31), _session(_PRIOR))
    service = _service(_source(), engine, [instrument])

    summary = await service.reconcile_completed(instrument, reference=_REFERENCE)
    assert _outcomes(summary).count(ReconciliationOutcome.RECONCILED) == 3
    for timeframe in (_FIVE, _SEVEN, _FIFTEEN):
        assert len(_finalized_of(engine, instrument, timeframe)) == 1


async def test_cache_coverage_means_no_source_call() -> None:
    engine = _engine([_FIVE])
    instrument = _instrument()
    engine.update(_tick(_PRIOR, 9, 16), _session(_PRIOR))
    engine.flush(datetime.combine(_PRIOR, time(9, 20), tzinfo=_IST).astimezone(UTC))
    source = _source()
    registry = InstrumentStateRegistry([instrument])
    coordinator = HistoricalCoordinator(source=source, cache=HistoricalCache(), max_concurrency=4)
    service = HistoricalWarmupService(
        registry=registry, coordinator=coordinator, planner=_planner(), candles=engine
    )
    # Pre-populate the cache with the exact session window the reconciler will use.
    start = datetime.combine(_PRIOR, time(9, 15), tzinfo=_IST).astimezone(UTC)
    end = datetime.combine(_PRIOR, time(15, 30), tzinfo=_IST).astimezone(UTC)
    await coordinator.fetch(
        HistoricalFetchPlan(
            instrument=instrument,
            requirement=HistoricalRequirement(timeframe=_FIVE, lookback=1),
            start=start,
            end=end,
            interval=interval_for_timeframe(_FIVE),
        )
    )
    calls_before = source.call_count
    await service.reconcile_completed(instrument, reference=_REFERENCE)
    assert source.call_count == calls_before  # served entirely from cache


async def test_source_unavailable_leaves_incomplete() -> None:
    engine = _engine([_FIVE])
    instrument = _instrument()
    engine.update(_tick(_PRIOR, 9, 16), _session(_PRIOR))
    engine.flush(datetime.combine(_PRIOR, time(9, 20), tzinfo=_IST).astimezone(UTC))
    service = _service(
        _source(by_interval={timedelta(minutes=5): Behavior.FAIL}), engine, [instrument]
    )

    summary = await service.reconcile_completed(instrument, reference=_REFERENCE)
    assert _outcomes(summary) == [ReconciliationOutcome.NO_AUTHORITATIVE_CANDLE]
    assert len(_incomplete_of(engine, instrument, _FIVE)) == 1  # untouched


async def test_partial_coverage_repairs_only_exact_matches() -> None:
    engine = _engine([_FIVE])
    instrument = _instrument()
    engine.update(_tick(_PRIOR, 9, 16), _session(_PRIOR))
    engine.update(_tick(_PRIOR, 9, 21), _session(_PRIOR))  # incompletes 09:15-09:20 and 09:20-09:25
    engine.flush(datetime.combine(_PRIOR, time(9, 25), tzinfo=_IST).astimezone(UTC))
    # INSUFFICIENT returns only the first 5m candle (09:15-09:20).
    service = _service(
        _source(by_interval={timedelta(minutes=5): Behavior.INSUFFICIENT}), engine, [instrument]
    )

    summary = await service.reconcile_completed(instrument, reference=_REFERENCE)
    outcomes = _outcomes(summary)
    assert outcomes.count(ReconciliationOutcome.RECONCILED) == 1
    assert outcomes.count(ReconciliationOutcome.NO_AUTHORITATIVE_CANDLE) == 1


# --------------------------------------------------------------------------- #
# Race / versioning (§44)
# --------------------------------------------------------------------------- #
def _wired() -> tuple[
    TickEngine, HistoricalWarmupService, CandleEngine, InstrumentStateRegistry, list[Event]
]:
    instrument = _instrument()
    engine = _engine([_FIVE])
    registry = InstrumentStateRegistry([instrument])
    bus = EventBus()
    recorded: list[Event] = []
    bus.subscribe(MarketContextCreated, recorded.append)
    bus.subscribe(MarketContextUpdated, recorded.append)
    classifier = MarketSessionClassifier(
        schedule=_SCHEDULE, calendar=TradingCalendar(), exchange_timezone=_TZ
    )
    coordinator = HistoricalCoordinator(
        source=_source(), cache=HistoricalCache(), max_concurrency=4
    )
    service = HistoricalWarmupService(
        registry=registry, coordinator=coordinator, planner=_planner(), candles=engine
    )
    tick_engine = TickEngine(
        registry=registry,
        bus=bus,
        clock=ManualClock(_REFERENCE),
        sequence=MonotonicSequence(),
        session=classifier,
        candles=engine,
    )
    return tick_engine, service, engine, registry, recorded


async def test_reconcile_creates_no_version_or_event() -> None:
    tick_engine, service, engine, registry, recorded = _wired()
    instrument = _instrument()
    engine.update(_tick(_PRIOR, 9, 16), _session(_PRIOR))
    engine.flush(datetime.combine(_PRIOR, time(9, 20), tzinfo=_IST).astimezone(UTC))

    await service.reconcile_completed(instrument, reference=_REFERENCE)
    assert recorded == []  # no event published by reconciliation
    assert registry.get(instrument) is None  # no MarketContext state/version minted


async def test_next_tick_surfaces_repaired_candle_with_one_version() -> None:
    tick_engine, service, engine, _registry, recorded = _wired()
    instrument = _instrument()
    engine.update(_tick(_PRIOR, 9, 16), _session(_PRIOR))
    engine.flush(datetime.combine(_PRIOR, time(9, 20), tzinfo=_IST).astimezone(UTC))
    await service.reconcile_completed(instrument, reference=_REFERENCE)

    today = _REFERENCE.astimezone(_IST).date()
    result = tick_engine.process(_tick(today, 9, 17))  # next accepted live datum (current session)
    assert result.context is not None
    assert result.context.version == 1
    assert len(recorded) == 1
    five = next(c for c in result.context.candle_sets if c.timeframe == _FIVE)
    assert len(five.finalized) == 1  # repaired candle still surfaced in candle_sets


async def test_reconciliation_is_deterministic() -> None:
    async def run() -> list[ReconciliationOutcome]:
        engine = _engine([_FIVE])
        instrument = _instrument()
        for minute in (16, 21, 26):
            engine.update(_tick(_PRIOR, 9, minute), _session(_PRIOR))
        service = _service(_source(), engine, [instrument])
        summary = await service.reconcile_completed(instrument, reference=_REFERENCE)
        return _outcomes(summary)

    assert await run() == await run()
