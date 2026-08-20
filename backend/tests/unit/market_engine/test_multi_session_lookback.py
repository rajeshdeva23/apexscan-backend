"""Multi-session historical lookback capability validation (ADR-007 MSH1-16; test-only).

Proves `HistoricalRequirement(Timeframe.session(), lookback=N)` (N>1) end-to-end through the
REAL historical pipeline — requirement union -> calendar-aware planner -> coordinator/cache ->
fake source -> session canonicalization -> HistoricalContext.series -> registry — with no
production code change. A tuple of requirements passed to the real HistoricalWarmupService IS
the test-only consumer; no production strategy, catalog entry, scanner, REST, or task is added.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.market_engine.historical.cache import HistoricalCache
from app.market_engine.historical.calendar_window import (
    CalendarCoverage,
    HistoricalCalendarWindow,
    OutsideCalendarCoverageError,
)
from app.market_engine.historical.context import HistoricalSeries
from app.market_engine.historical.coordinator import HistoricalCoordinator
from app.market_engine.historical.requirements import (
    HistoricalRequirement,
    HistoricalRequirementRegistry,
)
from app.market_engine.historical.service import (
    HistoricalRangePlanner,
    HistoricalWarmupService,
    WarmupState,
)
from app.market_engine.session import SessionSchedule, TradingCalendar
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument
from tests.fakes.historical_source import Behavior, FakeHistoricalSource

_SESSION = Timeframe.session()
_DIRECT = frozenset({_SESSION})
_REFERENCE = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)  # Mon 2026-08-10, 11:30 IST
_ANCHOR = date(2026, 8, 10)
_IST = ZoneInfo("Asia/Kolkata")
_SCHEDULE = SessionSchedule(
    pre_open_start=datetime(2000, 1, 1, 9, 0).time(),
    opening_auction_start=datetime(2000, 1, 1, 9, 8).time(),
    regular_open=datetime(2000, 1, 1, 9, 15).time(),
    regular_close=datetime(2000, 1, 1, 15, 30).time(),
    closing_end=datetime(2000, 1, 1, 15, 40).time(),
)
_FULL_COVERAGE = (date(2026, 1, 1), date(2026, 12, 31))


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _window(
    coverage: tuple[date, date] = _FULL_COVERAGE, calendar: TradingCalendar | None = None
) -> HistoricalCalendarWindow:
    return HistoricalCalendarWindow(
        calendar=calendar or TradingCalendar(),
        coverage=CalendarCoverage(start_date=coverage[0], end_date=coverage[1]),
    )


def _service(
    source: FakeHistoricalSource,
    instruments: list[Instrument],
    window: HistoricalCalendarWindow | None = None,
) -> tuple[HistoricalWarmupService, InstrumentStateRegistry]:
    registry = InstrumentStateRegistry(instruments)
    coordinator = HistoricalCoordinator(source=source, cache=HistoricalCache(), max_concurrency=4)
    planner = HistoricalRangePlanner(
        schedule=_SCHEDULE, exchange_timezone="Asia/Kolkata", calendar_window=window or _window()
    )
    return HistoricalWarmupService(
        registry=registry, coordinator=coordinator, planner=planner
    ), registry


def _source(**kwargs: object) -> FakeHistoricalSource:
    return FakeHistoricalSource(direct_timeframes=_DIRECT, **kwargs)  # type: ignore[arg-type]


def _session_series(
    registry: InstrumentStateRegistry, instrument: Instrument
) -> HistoricalSeries | None:
    state = registry.get(instrument)
    if state is None or state.historical is None:
        return None
    for series in state.historical.series:
        if series.timeframe.is_session:
            return series
    return None


def _session_dates(series: HistoricalSeries) -> list[date]:
    return [candle.start_timestamp.astimezone(_IST).date() for candle in series.candles]


async def _warm_session(lookback: int, *, window: HistoricalCalendarWindow | None = None):
    source = _source()
    instrument = _instrument()
    service, registry = _service(source, [instrument], window)
    status = await service.warmup(
        [instrument],
        (HistoricalRequirement(timeframe=_SESSION, lookback=lookback),),
        reference=_REFERENCE,
    )
    return status, registry, instrument, source


# --------------------------------------------------------------------------- #
# A/B/C, D, E, exact-length, invariants
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("lookback", [2, 5, 20])
async def test_lookback_returns_exactly_n_completed_sessions_oldest_to_newest(
    lookback: int,
) -> None:
    status, registry, instrument, _ = await _warm_session(lookback)
    assert status[instrument].state is WarmupState.SATISFIED
    series = _session_series(registry, instrument)
    assert series is not None
    assert len(series.candles) == lookback  # exact N, no truncation/padding
    dates = _session_dates(series)
    assert dates == sorted(dates)  # oldest -> newest
    assert len(set(dates)) == lookback  # unique sessions
    # matches the authoritative calendar's N previous trading days (oldest->newest)
    expected = list(_window().previous_trading_days(_ANCHOR, lookback))
    assert dates == expected
    # series[-1] == D-1 (previous completed session)
    assert dates[-1] == expected[-1]
    assert dates[-1] < _ANCHOR  # current day excluded


async def test_previous_session_matches_series_last() -> None:
    _, registry, instrument, _ = await _warm_session(5)
    state = registry.get(instrument)
    assert state is not None and state.historical is not None
    series = _session_series(registry, instrument)
    assert series is not None
    assert state.historical.previous_session is not None
    assert state.historical.previous_session.candle == series.candles[-1]


async def test_series_invariants_non_overlapping_single_instrument() -> None:
    _, registry, instrument, _ = await _warm_session(20)
    series = _session_series(registry, instrument)
    assert series is not None
    assert series.timeframe.is_session
    assert all(c.instrument == instrument for c in series.candles)
    for prev, cur in zip(series.candles, series.candles[1:], strict=False):
        assert cur.start_timestamp >= prev.end_timestamp  # non-overlapping, ascending


# --------------------------------------------------------------------------- #
# Calendar traversal (G/H/I/J)
# --------------------------------------------------------------------------- #
async def test_weekend_skipped() -> None:
    # anchor Mon 2026-08-10 -> D-1 must be Fri 2026-08-07 (Sat/Sun skipped).
    _, registry, instrument, _ = await _warm_session(2)
    dates = _session_dates(_session_series(registry, instrument))  # type: ignore[arg-type]
    assert dates == [date(2026, 8, 6), date(2026, 8, 7)]  # Thu, Fri
    assert all(d.weekday() < 5 for d in dates)  # no weekend


async def test_holiday_and_consecutive_closures_skipped() -> None:
    calendar = TradingCalendar(holidays=[date(2026, 8, 6), date(2026, 8, 5)])
    window = _window(calendar=calendar)
    _, registry, instrument, _ = await _warm_session(2, window=window)
    dates = _session_dates(_session_series(registry, instrument))  # type: ignore[arg-type]
    assert date(2026, 8, 6) not in dates and date(2026, 8, 5) not in dates
    assert dates == [date(2026, 8, 4), date(2026, 8, 7)]  # Tue, Fri (Wed/Thu holidays, Sat/Sun off)


async def test_exceptional_open_counts_as_one_session() -> None:
    calendar = TradingCalendar(open_sessions=[date(2026, 8, 8)])  # a Saturday
    window = _window(calendar=calendar)
    _, registry, instrument, _ = await _warm_session(2, window=window)
    dates = _session_dates(_session_series(registry, instrument))  # type: ignore[arg-type]
    assert date(2026, 8, 8) in dates  # exceptional OPEN Saturday included
    assert dates == [date(2026, 8, 7), date(2026, 8, 8)]  # Fri, Sat(open)


# --------------------------------------------------------------------------- #
# Current-day exclusion (F)
# --------------------------------------------------------------------------- #
async def test_no_session_on_or_after_anchor() -> None:
    _, registry, instrument, _ = await _warm_session(20)
    dates = _session_dates(_session_series(registry, instrument))  # type: ignore[arg-type]
    assert all(d < _ANCHOR for d in dates)


# --------------------------------------------------------------------------- #
# Requirement union (M)
# --------------------------------------------------------------------------- #
def test_requirement_union_takes_max_lookback_per_timeframe() -> None:
    registry = HistoricalRequirementRegistry()
    registry.register("cpr", frozenset({HistoricalRequirement(timeframe=_SESSION, lookback=1)}))
    registry.register("range", frozenset({HistoricalRequirement(timeframe=_SESSION, lookback=5)}))
    registry.register("nrk", frozenset({HistoricalRequirement(timeframe=_SESSION, lookback=20)}))
    effective = registry.effective_requirements()
    session = [r for r in effective if r.timeframe.is_session]
    assert len(session) == 1
    assert session[0].lookback == 20  # max(1,5,20), not sum


# --------------------------------------------------------------------------- #
# Cache containment + concurrency (N/O/P)
# --------------------------------------------------------------------------- #
async def test_cache_containment_serves_smaller_lookback_without_refetch() -> None:
    source = _source()
    instrument = _instrument()
    service, _ = _service(source, [instrument])
    await service.warmup(
        [instrument],
        (HistoricalRequirement(timeframe=_SESSION, lookback=20),),
        reference=_REFERENCE,
    )
    await service.warmup(
        [instrument], (HistoricalRequirement(timeframe=_SESSION, lookback=5),), reference=_REFERENCE
    )
    # the wider 20-session window (cached) fully contains the 5-session window -> no refetch
    assert source.call_count == 1


async def test_bounded_concurrency_respected() -> None:
    instruments = [_instrument(f"SYM{i:03d}") for i in range(12)]
    source = _source()
    service, _ = _service(source, instruments)
    await service.warmup(
        instruments, (HistoricalRequirement(timeframe=_SESSION, lookback=5),), reference=_REFERENCE
    )
    assert source.max_active <= 4  # coordinator Semaphore bound


# --------------------------------------------------------------------------- #
# Insufficient provider history vs coverage failure (Q/R) + no fabrication
# --------------------------------------------------------------------------- #
async def test_insufficient_provider_history_is_local_not_fabricated() -> None:
    source = _source(by_symbol={"RELIANCE": Behavior.INSUFFICIENT})
    instrument = _instrument()
    service, registry = _service(source, [instrument])
    status = await service.warmup(
        [instrument], (HistoricalRequirement(timeframe=_SESSION, lookback=5),), reference=_REFERENCE
    )
    assert status[instrument].state is not WarmupState.SATISFIED
    assert _session_series(registry, instrument) is None  # no truncated/fabricated series installed


async def test_calendar_coverage_insufficient_fails_closed() -> None:
    # coverage starts 2026-08-06; only Aug 6/7 available before anchor -> cannot supply 20.
    window = _window(coverage=(date(2026, 8, 6), date(2026, 12, 31)))
    source = _source()
    instrument = _instrument()
    service, _ = _service(source, [instrument], window)
    with pytest.raises(OutsideCalendarCoverageError):
        await service.warmup(
            [instrument],
            (HistoricalRequirement(timeframe=_SESSION, lookback=20),),
            reference=_REFERENCE,
        )


async def test_partial_universe_local_gap_does_not_fail_others() -> None:
    instruments = [_instrument("AAA"), _instrument("BBB"), _instrument("CCC")]
    source = _source(by_symbol={"BBB": Behavior.FAIL})
    service, registry = _service(source, instruments)
    status = await service.warmup(
        instruments, (HistoricalRequirement(timeframe=_SESSION, lookback=5),), reference=_REFERENCE
    )
    assert status[_instrument("AAA")].state is WarmupState.SATISFIED
    assert status[_instrument("CCC")].state is WarmupState.SATISFIED
    assert status[_instrument("BBB")].state is not WarmupState.SATISFIED
    assert len(_session_series(registry, _instrument("AAA")).candles) == 5  # type: ignore[union-attr]
    assert _session_series(registry, _instrument("BBB")) is None  # no fabrication


# --------------------------------------------------------------------------- #
# Determinism (V) + lookback=60 (AA)
# --------------------------------------------------------------------------- #
async def test_deterministic_repeated_warmup() -> None:
    _, reg1, inst, _ = await _warm_session(5)
    _, reg2, _, _ = await _warm_session(5)
    assert _session_series(reg1, inst).candles == _session_series(reg2, inst).candles  # type: ignore[union-attr]


async def test_lookback_60_offline_within_coverage() -> None:
    status, registry, instrument, _ = await _warm_session(60)
    assert status[instrument].state is WarmupState.SATISFIED
    series = _session_series(registry, instrument)
    assert series is not None and len(series.candles) == 60
    dates = _session_dates(series)
    assert dates == sorted(dates) and len(set(dates)) == 60
    assert dates == list(_window().previous_trading_days(_ANCHOR, 60))
