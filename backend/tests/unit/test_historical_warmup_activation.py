"""Historical-warmup activation over the packaged NSE 2026 dataset (ADR-011-IMPL; §14).

Proves the production activation path consumes the dataset as the single historical
calendar authority: coverage boundary, weekend-open Budget/Muhurat sessions, single-block
intraday planning, Muhurat intraday fail-closed, multi-interval preservation, lazy
zero-demand behaviour, current-day isolation, and provider neutrality. Pure/offline —
the planner and warmup service are built exactly as ``compose_market_runtime`` builds them,
from a fake historical source; no network, no wall clock.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

from app.core.config import Settings
from app.market_engine.calendar_data import (
    CalendarProvenance,
    IntervalSpec,
    SessionOverrideSpec,
    TradingCalendarDataset,
    load_nse_cm_2026_dataset,
)
from app.market_engine.historical.calendar_window import (
    CalendarCoverage,
    HistoricalCalendarWindow,
    MissingSessionTimingError,
    OutsideCalendarCoverageError,
)
from app.market_engine.historical.requirements import HistoricalRequirement
from app.market_engine.historical.service import HistoricalRangePlanner
from app.market_engine.session import TradingInterval
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument
from app.services.historical_source_bridge import DHAN_DIRECT_TIMEFRAMES
from app.services.market_runtime import _schedule_and_calendar
from app.services.strategy_requirements_wiring import build_historical_warmup_service
from tests.architecture.import_boundary import forbidden_imports
from tests.fakes.historical_source import FakeHistoricalSource

_DB = "postgresql+asyncpg://user:pass@localhost:5432/apexscan"
_REDIS = "redis://localhost:6379/0"
_5M = Timeframe.minutes(5)
_REGULAR_OPEN = time(9, 15)
_REGULAR_CLOSE = time(15, 30)


def _settings() -> Settings:
    return Settings(app_env="development", database_url=_DB, redis_url=_REDIS)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _planner(dataset: TradingCalendarDataset) -> HistoricalRangePlanner:
    """Build the planner exactly as composition does: dataset calendar/coverage/overrides."""
    settings = _settings()
    schedule, _ = _schedule_and_calendar(settings)
    window = HistoricalCalendarWindow(
        calendar=dataset.trading_calendar(), coverage=dataset.calendar_coverage()
    )
    return HistoricalRangePlanner(
        schedule=schedule,
        exchange_timezone=settings.exchange_timezone,
        calendar_window=window,
        overrides=dataset.session_overrides_domain(),
    )


def _reference(anchor: date) -> datetime:
    """Return a UTC instant whose IST (UTC+5:30) trading date is ``anchor``."""
    return datetime(anchor.year, anchor.month, anchor.day, 6, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# §14 B — coverage boundary is exactly the provisioned 2026 window
# --------------------------------------------------------------------------- #
def test_coverage_is_exactly_the_2026_window() -> None:
    dataset = load_nse_cm_2026_dataset()
    assert dataset.calendar_coverage() == CalendarCoverage(
        start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)
    )


# --------------------------------------------------------------------------- #
# §14 C — weekend-open Budget/Muhurat trade; closed dates do not
# --------------------------------------------------------------------------- #
def test_special_open_and_closed_dates_are_classified() -> None:
    calendar = load_nse_cm_2026_dataset().trading_calendar()
    assert calendar.is_trading_day(date(2026, 2, 1)) is True  # Budget (Sunday)
    assert calendar.is_trading_day(date(2026, 11, 8)) is True  # Muhurat (Sunday)
    assert calendar.is_trading_day(date(2026, 1, 15)) is False  # amendment closure
    assert calendar.is_trading_day(date(2026, 1, 26)) is False  # Republic Day


# --------------------------------------------------------------------------- #
# §14 D — 2026-02-01 intraday planning uses the single 09:15-15:30 interval
# --------------------------------------------------------------------------- #
def test_budget_session_uses_single_regular_interval() -> None:
    planner = _planner(load_nse_cm_2026_dataset())
    intervals = planner.effective_schedule.intervals_for(date(2026, 2, 1))
    assert intervals == (TradingInterval(start=_REGULAR_OPEN, end=_REGULAR_CLOSE),)


def test_budget_intraday_window_resolves_without_fabrication() -> None:
    planner = _planner(load_nse_cm_2026_dataset())
    requirement = HistoricalRequirement(timeframe=_5M, lookback=3)
    start, end = planner.resolve(requirement, _reference(date(2026, 2, 2)))
    assert start.tzinfo is not None and end.tzinfo is not None
    assert start < end


# --------------------------------------------------------------------------- #
# §14 E — Muhurat: session/daily resolves; intraday fails closed
# --------------------------------------------------------------------------- #
def test_muhurat_session_resolves_but_intraday_fails_closed() -> None:
    planner = _planner(load_nse_cm_2026_dataset())
    reference = _reference(date(2026, 11, 9))  # Monday after the Sunday Muhurat
    session_req = HistoricalRequirement(timeframe=Timeframe.session(), lookback=1)
    start, end = planner.resolve(session_req, reference)  # session/daily authoritative
    assert start < end
    intraday_req = HistoricalRequirement(timeframe=_5M, lookback=3)
    with pytest.raises(MissingSessionTimingError):
        planner.resolve(intraday_req, reference)


# --------------------------------------------------------------------------- #
# §14 F — a requirement reaching before coverage fails closed
# --------------------------------------------------------------------------- #
def test_requirement_before_coverage_start_fails_closed() -> None:
    planner = _planner(load_nse_cm_2026_dataset())
    requirement = HistoricalRequirement(timeframe=Timeframe.session(), lookback=1)
    with pytest.raises(OutsideCalendarCoverageError):
        planner.resolve(requirement, _reference(date(2026, 1, 1)))


# --------------------------------------------------------------------------- #
# §14 G — zero historical demand → zero source calls (warmup stays lazy)
# --------------------------------------------------------------------------- #
async def test_zero_demand_makes_no_source_calls() -> None:
    dataset = load_nse_cm_2026_dataset()
    settings = _settings()
    schedule, _ = _schedule_and_calendar(settings)
    source = FakeHistoricalSource(direct_timeframes=DHAN_DIRECT_TIMEFRAMES)
    service = build_historical_warmup_service(
        source=source,
        registry=InstrumentStateRegistry((_instrument(),)),
        schedule=schedule,
        exchange_timezone=settings.exchange_timezone,
        calendar=dataset.trading_calendar(),
        coverage=dataset.calendar_coverage(),
        overrides=dataset.session_overrides_domain(),
    )
    await service.warmup((_instrument(),), (), reference=_reference(date(2026, 8, 6)))
    assert source.call_count == 0


# --------------------------------------------------------------------------- #
# §14 H — composed warmup keeps supports_current_day False
# --------------------------------------------------------------------------- #
def test_warmup_service_withholds_current_day() -> None:
    dataset = load_nse_cm_2026_dataset()
    settings = _settings()
    schedule, _ = _schedule_and_calendar(settings)
    service = build_historical_warmup_service(
        source=FakeHistoricalSource(direct_timeframes=DHAN_DIRECT_TIMEFRAMES),
        registry=InstrumentStateRegistry(()),
        schedule=schedule,
        exchange_timezone=settings.exchange_timezone,
        calendar=dataset.trading_calendar(),
        coverage=dataset.calendar_coverage(),
        overrides=dataset.session_overrides_domain(),
    )
    assert service._supports_current_day is False  # noqa: SLF001


# --------------------------------------------------------------------------- #
# Multi-interval preservation — the interval tuple flows through uncollapsed
# --------------------------------------------------------------------------- #
def _multi_interval_dataset() -> TradingCalendarDataset:
    special = date(2026, 3, 14)
    return TradingCalendarDataset(
        dataset_id="synthetic_multi",
        version="0.0.1",
        segment="NSE_EQ",
        coverage_start=date(2026, 1, 1),
        coverage_end=date(2026, 12, 31),
        closed_dates=(),
        open_sessions=(special,),
        session_overrides=(
            SessionOverrideSpec(
                trading_date=special,
                intervals=(
                    IntervalSpec(start=time(9, 15), end=time(10, 0)),
                    IntervalSpec(start=time(11, 30), end=time(12, 30)),
                ),
            ),
        ),
        provenance=(
            CalendarProvenance(
                circular_id="TEST/1",
                circular_date=date(2026, 1, 1),
                segment="Capital Market",
                fact="synthetic disjoint two-block open session on 2026-03-14",
            ),
        ),
    )


def test_multi_interval_override_is_not_collapsed() -> None:
    planner = _planner(_multi_interval_dataset())
    intervals = planner.effective_schedule.intervals_for(date(2026, 3, 14))
    assert intervals == (
        TradingInterval(start=time(9, 15), end=time(10, 0)),
        TradingInterval(start=time(11, 30), end=time(12, 30)),
    )
    # The whole-session envelope spans the blocks but never replaces the intraday tuple.
    envelope = planner.effective_schedule.envelope_for(date(2026, 3, 14))
    assert envelope == TradingInterval(start=time(9, 15), end=time(12, 30))


# --------------------------------------------------------------------------- #
# §14 K — provider neutrality: calendar/historical types acquire no Dhan
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "module_path",
    [
        "app/market_engine/calendar_data/dataset.py",
        "app/market_engine/calendar_data/loader.py",
        "app/market_engine/historical/calendar_window.py",
        "app/market_engine/historical/service.py",
    ],
)
def test_calendar_and_historical_types_import_no_dhan(module_path: str) -> None:
    app_root = Path(__file__).parents[2]
    source = (app_root / module_path).read_text(encoding="utf-8")
    package = ".".join(Path(module_path).with_suffix("").parts[:-1])
    assert forbidden_imports(source, package=package) == []
