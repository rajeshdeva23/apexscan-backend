"""Capability classification and deterministic fetch planning (P4.5B; §40)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime

import pytest

from app.market_engine.historical.calendar_window import CalendarCoverage, HistoricalCalendarWindow
from app.market_engine.historical.requirements import (
    HistoricalRequirement,
    HistoricalRequirementRegistry,
)
from app.market_engine.historical.service import HistoricalRangePlanner, plan_direct_fetches
from app.market_engine.session import SessionSchedule, TradingCalendar
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument

_REFERENCE = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)  # Monday, 11:30 IST (live session)
_DIRECT = frozenset(
    {
        Timeframe.minutes(1),
        Timeframe.minutes(5),
        Timeframe.minutes(15),
        Timeframe.minutes(25),
        Timeframe.minutes(60),
        Timeframe.session(),
    }
)
_SCHEDULE = SessionSchedule(
    pre_open_start=datetime(2000, 1, 1, 9, 0).time(),
    opening_auction_start=datetime(2000, 1, 1, 9, 8).time(),
    regular_open=datetime(2000, 1, 1, 9, 15).time(),
    regular_close=datetime(2000, 1, 1, 15, 30).time(),
    closing_end=datetime(2000, 1, 1, 15, 40).time(),
)


def _planner() -> HistoricalRangePlanner:
    window = HistoricalCalendarWindow(
        calendar=TradingCalendar(),
        coverage=CalendarCoverage(start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)),
    )
    return HistoricalRangePlanner(
        schedule=_SCHEDULE, exchange_timezone="Asia/Kolkata", calendar_window=window
    )


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _plan(instruments: list[Instrument], requirements: tuple[HistoricalRequirement, ...]):  # noqa: ANN202
    return plan_direct_fetches(
        instruments=instruments,
        effective_requirements=requirements,
        direct_timeframes=_DIRECT,
        planner=_planner(),
        reference=_REFERENCE,
    )


@pytest.mark.parametrize(
    "timeframe",
    [Timeframe.minutes(1), Timeframe.minutes(5), Timeframe.minutes(15), Timeframe.session()],
)
def test_direct_timeframes_produce_a_plan(timeframe: Timeframe) -> None:
    plans = _plan([_instrument()], (HistoricalRequirement(timeframe=timeframe, lookback=10),))
    assert len(plans) == 1
    assert plans[0].requirement.timeframe == timeframe
    assert plans[0].start.tzinfo is UTC
    assert plans[0].start < plans[0].end


def test_non_direct_timeframe_produces_no_plan() -> None:
    plans = _plan(
        [_instrument()], (HistoricalRequirement(timeframe=Timeframe.minutes(7), lookback=30),)
    )
    assert plans == ()


def test_mixed_requirements_plan_only_direct_timeframes() -> None:
    requirements = (
        HistoricalRequirement(timeframe=Timeframe.minutes(5), lookback=100),
        HistoricalRequirement(timeframe=Timeframe.minutes(7), lookback=50),
        HistoricalRequirement(timeframe=Timeframe.session(), lookback=20),
    )
    plans = _plan([_instrument()], requirements)
    assert {plan.requirement.timeframe for plan in plans} == {
        Timeframe.minutes(5),
        Timeframe.session(),
    }


def test_plan_ordering_is_deterministic() -> None:
    instruments = [_instrument("ZEEL"), _instrument("ADANIENT")]
    requirements = (
        HistoricalRequirement(timeframe=Timeframe.minutes(15), lookback=5),
        HistoricalRequirement(timeframe=Timeframe.minutes(5), lookback=5),
    )
    plans = _plan(instruments, requirements)
    order = [(plan.instrument.symbol, plan.requirement.timeframe.label) for plan in plans]
    assert order == [
        ("ADANIENT", "300s"),
        ("ADANIENT", "900s"),
        ("ZEEL", "300s"),
        ("ZEEL", "900s"),
    ]


def test_plan_carries_no_consumer_key_or_strategy() -> None:
    plans = _plan(
        [_instrument()], (HistoricalRequirement(timeframe=Timeframe.minutes(5), lookback=10),)
    )
    field_names = {field.name for field in dataclasses.fields(plans[0])}
    assert field_names == {"instrument", "requirement", "start", "end", "interval"}


def test_208_instruments_times_3_direct_requirements_is_624_plans() -> None:
    instruments = [_instrument(f"SYM{index:03d}") for index in range(208)]
    requirements = (
        HistoricalRequirement(timeframe=Timeframe.minutes(5), lookback=100),
        HistoricalRequirement(timeframe=Timeframe.minutes(15), lookback=50),
        HistoricalRequirement(timeframe=Timeframe.session(), lookback=20),
    )
    plans = _plan(instruments, requirements)
    assert len(plans) == 624


def test_consumer_union_collapse_yields_one_plan_per_instrument() -> None:
    registry = HistoricalRequirementRegistry()
    registry.register("a", [HistoricalRequirement(timeframe=Timeframe.minutes(5), lookback=20)])
    registry.register("b", [HistoricalRequirement(timeframe=Timeframe.minutes(5), lookback=100)])
    registry.register("c", [HistoricalRequirement(timeframe=Timeframe.minutes(5), lookback=50)])
    instruments = [_instrument(f"SYM{index:03d}") for index in range(208)]
    plans = _plan(instruments, registry.effective_requirements())
    assert len(plans) == 208  # not 624 — union collapsed 5m to a single requirement
    assert {plan.requirement.lookback for plan in plans} == {100}
