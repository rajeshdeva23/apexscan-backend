"""Assertions over the real, packaged NSE 2026 Capital-Market calendar dataset.

Identity, closed/open/exception classification, per-date timing with H3 intraday
fail-closed for the Muhurat session, deterministic offline loading, and isolation
from current-day / session-statistics authority (ADR-011-DATA-R1). The dataset values
themselves are the authoritative 2026 evidence-record data; only planner-window
reference instants are chosen here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest

from app.market_engine.calendar_data import load_nse_cm_2026_dataset
from app.market_engine.historical.calendar_window import (
    HistoricalCalendarWindow,
    MissingSessionTimingError,
)
from app.market_engine.historical.requirements import HistoricalRequirement
from app.market_engine.historical.service import HistoricalRangePlanner
from app.market_engine.session import SessionSchedule, TradingInterval
from app.market_engine.session_statistics import SessionStatisticsAuthority
from app.market_engine.timeframe import Timeframe
from tests.architecture.import_boundary import imported_modules

_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)

_CLOSED_DATES = (
    date(2026, 1, 15),
    date(2026, 1, 26),
    date(2026, 3, 3),
    date(2026, 3, 26),
    date(2026, 3, 31),
    date(2026, 4, 3),
    date(2026, 4, 14),
    date(2026, 5, 1),
    date(2026, 5, 28),
    date(2026, 6, 26),
    date(2026, 9, 14),
    date(2026, 10, 2),
    date(2026, 10, 20),
    date(2026, 11, 10),
    date(2026, 11, 24),
    date(2026, 12, 25),
)
_BUDGET_OPEN = date(2026, 2, 1)
_MUHURAT_OPEN = date(2026, 11, 8)
_WEEKEND_HOLIDAYS = (date(2026, 2, 15), date(2026, 3, 21), date(2026, 8, 15))


def _planner() -> HistoricalRangePlanner:
    dataset = load_nse_cm_2026_dataset()
    window = HistoricalCalendarWindow(
        calendar=dataset.trading_calendar(), coverage=dataset.calendar_coverage()
    )
    return HistoricalRangePlanner(
        schedule=_SCHEDULE,
        exchange_timezone="Asia/Kolkata",
        calendar_window=window,
        overrides=dataset.session_overrides_domain(),
    )


# --------------------------------------------------------------------------- #
# A. Identity
# --------------------------------------------------------------------------- #
def test_dataset_identity_and_counts() -> None:
    dataset = load_nse_cm_2026_dataset()
    assert dataset.version
    coverage = dataset.calendar_coverage()
    assert coverage.start_date == date(2026, 1, 1)
    assert coverage.end_date == date(2026, 12, 31)
    assert len(dataset.closed_dates) == 16
    assert len(dataset.open_sessions) == 2
    assert len(dataset.session_overrides) == 1


# --------------------------------------------------------------------------- #
# B. Closed dates and weekend rule
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("closed", _CLOSED_DATES, ids=[d.isoformat() for d in _CLOSED_DATES])
def test_each_closed_date_is_not_a_trading_day(closed: date) -> None:
    assert load_nse_cm_2026_dataset().trading_calendar().is_trading_day(closed) is False


@pytest.mark.parametrize("ordinary", [date(2026, 2, 2), date(2026, 7, 1)])
def test_representative_ordinary_weekday_is_a_trading_day(ordinary: date) -> None:
    assert load_nse_cm_2026_dataset().trading_calendar().is_trading_day(ordinary) is True


@pytest.mark.parametrize("weekend_holiday", _WEEKEND_HOLIDAYS)
def test_category_b_weekend_holiday_is_closed_by_weekend_rule_only(weekend_holiday: date) -> None:
    calendar = load_nse_cm_2026_dataset().trading_calendar()
    assert calendar.is_trading_day(weekend_holiday) is False
    assert weekend_holiday not in calendar.closed_dates


# --------------------------------------------------------------------------- #
# C. Exceptional OPEN sessions (both Sundays)
# --------------------------------------------------------------------------- #
def test_exceptional_open_sessions_are_trading_days() -> None:
    calendar = load_nse_cm_2026_dataset().trading_calendar()
    assert _BUDGET_OPEN.weekday() == 6
    assert _MUHURAT_OPEN.weekday() == 6
    assert calendar.is_trading_day(_BUDGET_OPEN) is True
    assert calendar.is_trading_day(_MUHURAT_OPEN) is True


# --------------------------------------------------------------------------- #
# D. Per-date timing and H3 intraday fail-closed
# --------------------------------------------------------------------------- #
def test_budget_override_intervals_and_muhurat_has_no_override() -> None:
    dataset = load_nse_cm_2026_dataset()
    effective = dataset.effective_schedule(_SCHEDULE)
    assert effective.intervals_for(_BUDGET_OPEN) == (
        TradingInterval(start=time(9, 15), end=time(15, 30)),
    )
    assert effective.has_override(_MUHURAT_OPEN) is False


def test_intraday_window_over_muhurat_fails_closed() -> None:
    # 2026-11-09 is Monday; walking back reaches OPEN Muhurat 2026-11-08 (no timing).
    reference = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    planner = _planner()
    assert planner.newest_completed_session(reference) == _MUHURAT_OPEN
    requirement = HistoricalRequirement(timeframe=Timeframe.minutes(5), lookback=10)
    with pytest.raises(MissingSessionTimingError):
        planner.resolve(requirement, reference)


def test_session_window_over_muhurat_resolves() -> None:
    reference = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    start, end = _planner().resolve(
        HistoricalRequirement(timeframe=Timeframe.session(), lookback=2), reference
    )
    assert start < end


# --------------------------------------------------------------------------- #
# F. Determinism / offline
# --------------------------------------------------------------------------- #
def test_loading_twice_yields_equal_canonical_domain_objects() -> None:
    first = load_nse_cm_2026_dataset()
    second = load_nse_cm_2026_dataset()
    assert first == second
    assert first.calendar_coverage() == second.calendar_coverage()
    assert first.trading_calendar().closed_dates == second.trading_calendar().closed_dates
    assert first.trading_calendar().open_sessions == second.trading_calendar().open_sessions
    assert first.session_overrides_domain() == second.session_overrides_domain()


def test_loader_imports_no_clock_or_network_modules() -> None:
    from app.market_engine.calendar_data import loader

    source = Path(loader.__file__).read_text(encoding="utf-8")
    modules = set(imported_modules(source, package="app.market_engine.calendar_data"))
    # No wall-clock (datetime) and no network transport is imported at all.
    for banned in ("datetime", "time", "httpx", "requests", "socket", "urllib", "websockets"):
        assert banned not in modules
        assert not any(module.startswith(f"{banned}.") for module in modules)


# --------------------------------------------------------------------------- #
# G. Isolation from current-day / session-statistics authority
# --------------------------------------------------------------------------- #
_PACKAGE_DIR = Path(load_nse_cm_2026_dataset.__module__.replace(".", "/")).parent
_APP_ROOT = Path(__file__).parents[4] / "app"
_FORBIDDEN_IMPORT_PREFIXES = (
    "app.adapters",
    "app.api",
    "app.database",
    "app.repositories",
    "app.strategies",
    "app.strategy_manager",
    "websockets",
    "pyotp",
)


def test_new_package_imports_no_provider_persistence_or_strategy() -> None:
    package_dir = _APP_ROOT / "market_engine" / "calendar_data"
    offenders: dict[str, list[str]] = {}
    for path in sorted(package_dir.rglob("*.py")):
        relative = path.relative_to(_APP_ROOT.parent).with_suffix("")
        package = ".".join(relative.parts[:-1])
        bad = [
            module
            for module in imported_modules(path.read_text(encoding="utf-8"), package=package)
            if any(module == p or module.startswith(f"{p}.") for p in _FORBIDDEN_IMPORT_PREFIXES)
        ]
        if bad:
            offenders[str(path)] = bad
    assert offenders == {}


def test_session_statistics_authority_defaults_stay_false() -> None:
    authority = SessionStatisticsAuthority()
    assert authority.staged_observation_verified is False
    assert authority.tick_aggregate_verified is False


def test_current_day_and_authority_bits_never_enabled_in_app() -> None:
    import re

    patterns = [
        re.compile(rf"{bit}\s*=\s*True")
        for bit in (
            "supports_current_day",
            "staged_observation_verified",
            "tick_aggregate_verified",
        )
    ]
    offenders: list[str] = []
    for path in sorted(_APP_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in patterns):
            offenders.append(str(path))
    assert offenders == []


async def test_production_composition_port_stays_fail_closed() -> None:
    from app.services.strategy_requirements_wiring import (
        HistoricalWarmupUnavailableError,
        UnavailableHistoricalWarmup,
    )

    port = UnavailableHistoricalWarmup()
    with pytest.raises(HistoricalWarmupUnavailableError):
        await port.warmup(
            [],
            [HistoricalRequirement(timeframe=Timeframe.session(), lookback=1)],
            reference=datetime(2026, 11, 9, 10, 0, tzinfo=UTC),
        )
