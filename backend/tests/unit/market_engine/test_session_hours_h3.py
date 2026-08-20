"""H3 partial authority: per-date session hours and fail-closed intraday timing.

Exercises ``HistoricalRangePlanner.resolve`` and the warmup service over synthetic
exceptional OPEN sessions (a weekend-open Saturday). Session/daily demand resolves
with an OPEN override (M17); intraday demand fails closed when the special session
lacks per-date session-hours metadata (M11/M16) and otherwise recomputes its window
from each date's effective intraday capacity (M15). No real NSE dates are used.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.market_engine.historical.cache import HistoricalCache
from app.market_engine.historical.calendar_window import (
    CalendarCoverage,
    HistoricalCalendarWindow,
    MissingSessionTimingError,
)
from app.market_engine.historical.coordinator import HistoricalCoordinator
from app.market_engine.historical.requirements import HistoricalRequirement
from app.market_engine.historical.service import (
    HistoricalRangePlanner,
    HistoricalWarmupService,
    WarmupState,
)
from app.market_engine.session import SessionSchedule, TradingCalendar, TradingSessionOverride
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument
from tests.fakes.historical_source import FakeHistoricalSource

_IST = ZoneInfo("Asia/Kolkata")
_TZ = "Asia/Kolkata"
_SATURDAY = date(2026, 8, 8)  # exceptional OPEN session
_MONDAY = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)  # 11:30 IST Monday
_TUESDAY = datetime(2026, 8, 11, 6, 0, tzinfo=UTC)  # 11:30 IST Tuesday
_FIVE = Timeframe.minutes(5)
_SESSION = Timeframe.session()
_DIRECT = frozenset({_FIVE, _SESSION})

_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)


def _override(start: time, end: time) -> TradingSessionOverride:
    return TradingSessionOverride.continuous(trading_date=_SATURDAY, start=start, end=end)


def _planner(
    *,
    open_sessions: tuple[date, ...] = (),
    overrides: tuple[TradingSessionOverride, ...] = (),
) -> HistoricalRangePlanner:
    window = HistoricalCalendarWindow(
        calendar=TradingCalendar(open_sessions=open_sessions),
        coverage=CalendarCoverage(start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)),
    )
    return HistoricalRangePlanner(
        schedule=_SCHEDULE, exchange_timezone=_TZ, calendar_window=window, overrides=overrides
    )


def _ist(day: date, moment: time) -> datetime:
    return datetime.combine(day, moment, tzinfo=_IST).astimezone(UTC)


# --------------------------------------------------------------------------- #
# (A) OPEN + timing + intraday: boundaries use override bounds
# --------------------------------------------------------------------------- #
def test_open_with_timing_intraday_uses_override_bounds() -> None:
    planner = _planner(open_sessions=(_SATURDAY,), overrides=(_override(time(10, 0), time(14, 0)),))
    start, end = planner.resolve(HistoricalRequirement(timeframe=_FIVE, lookback=10), _MONDAY)
    # Newest resolved session is the OPEN Saturday, closed at its override 14:00.
    assert end == _ist(_SATURDAY, time(14, 0))
    # Oldest session is ordinary Friday, opened at the default 09:15.
    assert start == _ist(date(2026, 8, 7), time(9, 15))


# --------------------------------------------------------------------------- #
# (B) OPEN + no timing + intraday: fail closed
# --------------------------------------------------------------------------- #
def test_open_without_timing_intraday_fails_closed() -> None:
    planner = _planner(open_sessions=(_SATURDAY,))
    with pytest.raises(MissingSessionTimingError, match="session-hours"):
        planner.resolve(HistoricalRequirement(timeframe=_FIVE, lookback=10), _MONDAY)


# --------------------------------------------------------------------------- #
# (C) OPEN + no timing + session/daily: allowed (M17)
# --------------------------------------------------------------------------- #
def test_open_without_timing_session_is_allowed() -> None:
    planner = _planner(open_sessions=(_SATURDAY,))
    start, end = planner.resolve(HistoricalRequirement(timeframe=_SESSION, lookback=2), _MONDAY)
    # Session identity uses default bounds even for the OPEN Saturday (M17).
    assert end == _ist(_SATURDAY, time(15, 30))
    assert start == _ist(date(2026, 8, 7), time(9, 15))


# --------------------------------------------------------------------------- #
# (D) Ordinary intraday: unchanged, byte-identical window
# --------------------------------------------------------------------------- #
def test_ordinary_intraday_window_unchanged() -> None:
    planner = _planner()  # no open sessions, no overrides
    start, end = planner.resolve(HistoricalRequirement(timeframe=_FIVE, lookback=10), _MONDAY)
    # ceil(10/75)+1 margin = 2 sessions: Thursday..Friday, default bounds.
    assert start == _ist(date(2026, 8, 6), time(9, 15))
    assert end == _ist(date(2026, 8, 7), time(15, 30))


# --------------------------------------------------------------------------- #
# (E) Shortened exceptional session: capacity uses override, not regular
# --------------------------------------------------------------------------- #
def test_shortened_session_capacity_uses_override_schedule() -> None:
    # A 1-hour Saturday (12 five-minute candles) cannot cover lookback=20 alone, so
    # the window reaches an extra session further back than a full-length session would.
    short = _planner(open_sessions=(_SATURDAY,), overrides=(_override(time(10, 0), time(11, 0)),))
    start, _ = short.resolve(HistoricalRequirement(timeframe=_FIVE, lookback=20), _MONDAY)
    assert start == _ist(date(2026, 8, 6), time(9, 15))  # reaches Thursday

    full = _planner(open_sessions=(_SATURDAY,), overrides=(_override(time(10, 0), time(14, 0)),))
    start_full, _ = full.resolve(HistoricalRequirement(timeframe=_FIVE, lookback=20), _MONDAY)
    assert start_full == _ist(date(2026, 8, 7), time(9, 15))  # only reaches Friday


# --------------------------------------------------------------------------- #
# (F) Later ordinary session uses default schedule (no leakage)
# --------------------------------------------------------------------------- #
def test_later_ordinary_session_uses_default_no_leakage() -> None:
    planner = _planner(open_sessions=(_SATURDAY,), overrides=(_override(time(10, 0), time(14, 0)),))
    _, end = planner.resolve(HistoricalRequirement(timeframe=_FIVE, lookback=100), _TUESDAY)
    # Newest resolved session is ordinary Monday: its close is the default 15:30,
    # never the Saturday override's 14:00.
    assert end == _ist(date(2026, 8, 10), time(15, 30))


# --------------------------------------------------------------------------- #
# Warmup-service fail-closed vs. resolvable (M11 at the service seam)
# --------------------------------------------------------------------------- #
def _service(planner: HistoricalRangePlanner) -> HistoricalWarmupService:
    instrument = Instrument(exchange="NSE", symbol="RELIANCE")
    registry = InstrumentStateRegistry([instrument])
    source = FakeHistoricalSource(direct_timeframes=_DIRECT)
    coordinator = HistoricalCoordinator(source=source, cache=HistoricalCache(), max_concurrency=4)
    return HistoricalWarmupService(registry=registry, coordinator=coordinator, planner=planner)


async def test_warmup_intraday_missing_timing_is_failed_not_satisfied() -> None:
    instrument = Instrument(exchange="NSE", symbol="RELIANCE")
    service = _service(_planner(open_sessions=(_SATURDAY,)))
    status = await service.warmup(
        [instrument], (HistoricalRequirement(timeframe=_FIVE, lookback=10),), reference=_MONDAY
    )
    assert status[instrument].state is WarmupState.FAILED
    assert status[instrument].unresolved == (_FIVE,)
    assert status[instrument].satisfied == ()
    assert status[instrument].pending_reconstruction == ()


async def test_warmup_intraday_with_timing_resolves() -> None:
    instrument = Instrument(exchange="NSE", symbol="RELIANCE")
    planner = _planner(open_sessions=(_SATURDAY,), overrides=(_override(time(10, 0), time(14, 0)),))
    service = _service(planner)
    status = await service.warmup(
        [instrument], (HistoricalRequirement(timeframe=_FIVE, lookback=10),), reference=_MONDAY
    )
    assert status[instrument].state is WarmupState.SATISFIED
    assert status[instrument].satisfied == (_FIVE,)
