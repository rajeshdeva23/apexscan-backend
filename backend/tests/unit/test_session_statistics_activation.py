"""SessionStatisticsRefreshCoordinator cadence/control + coordinator wiring (P4.6E5)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import pytest

from app.adapters.base import ProviderUnavailableError
from app.market_engine.historical.requirements import HistoricalRequirementRegistry
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument
from app.services.session_statistics_activation import SessionStatisticsRefreshCoordinator
from app.strategies.enums import CandleCompleteness, FactNeed, StrategyTrigger
from app.strategies.requirements import FactFreshnessRequirement, StrategyRequirements
from app.strategy_manager.live_timeframes import LiveTimeframeRequirementRegistry
from app.strategy_manager.ports import SessionStatisticsRefreshControl
from app.strategy_manager.requirements_bridge import RequirementsCoordinator

_DATE = date(2026, 8, 6)
_T0 = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)
_5S = timedelta(seconds=5)
_3S = timedelta(seconds=3)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


@dataclass
class _RecordingRefreshService:
    """A stand-in for the E4 refresh service that records each driven batch."""

    calls: list[tuple[tuple[Instrument, ...], date]] = field(default_factory=list)
    error: Exception | None = None

    async def refresh(self, instruments: Sequence[Instrument], *, trading_date: date) -> object:
        self.calls.append((tuple(instruments), trading_date))
        if self.error is not None:
            raise self.error
        return None


def _coordinator(
    service: _RecordingRefreshService, *, symbols: Sequence[str] = ("RELIANCE",)
) -> SessionStatisticsRefreshCoordinator:
    return SessionStatisticsRefreshCoordinator(
        service=service,  # type: ignore[arg-type]
        instruments=[_instrument(s) for s in symbols],
    )


# --------------------------------------------------------------------------- #
# Cadence
# --------------------------------------------------------------------------- #
async def test_inactive_coordinator_does_not_refresh() -> None:
    service = _RecordingRefreshService()
    coordinator = _coordinator(service)
    assert await coordinator.refresh_if_due(reference=_T0, trading_date=_DATE) is False
    assert service.calls == []


async def test_first_active_invocation_refreshes() -> None:
    service = _RecordingRefreshService()
    coordinator = _coordinator(service)
    coordinator.configure(max_age=_5S)
    assert await coordinator.refresh_if_due(reference=_T0, trading_date=_DATE) is True
    assert len(service.calls) == 1


async def test_before_due_does_not_refresh() -> None:
    service = _RecordingRefreshService()
    coordinator = _coordinator(service)
    coordinator.configure(max_age=_5S)
    await coordinator.refresh_if_due(reference=_T0, trading_date=_DATE)
    assert await coordinator.refresh_if_due(reference=_T0 + _3S, trading_date=_DATE) is False
    assert len(service.calls) == 1


async def test_exactly_due_refreshes() -> None:
    service = _RecordingRefreshService()
    coordinator = _coordinator(service)
    coordinator.configure(max_age=_5S)
    await coordinator.refresh_if_due(reference=_T0, trading_date=_DATE)
    assert await coordinator.refresh_if_due(reference=_T0 + _5S, trading_date=_DATE) is True
    assert len(service.calls) == 2


async def test_after_due_refreshes() -> None:
    service = _RecordingRefreshService()
    coordinator = _coordinator(service)
    coordinator.configure(max_age=_5S)
    await coordinator.refresh_if_due(reference=_T0, trading_date=_DATE)
    assert (
        await coordinator.refresh_if_due(reference=_T0 + timedelta(seconds=9), trading_date=_DATE)
        is True
    )


async def test_stricter_cadence_after_reconfigure() -> None:
    service = _RecordingRefreshService()
    coordinator = _coordinator(service)
    coordinator.configure(max_age=_5S)
    await coordinator.refresh_if_due(reference=_T0, trading_date=_DATE)
    coordinator.configure(max_age=_3S)  # a stricter consumer joined
    assert await coordinator.refresh_if_due(reference=_T0 + _3S, trading_date=_DATE) is True


async def test_deactivation_stops_refresh() -> None:
    service = _RecordingRefreshService()
    coordinator = _coordinator(service)
    coordinator.configure(max_age=_5S)
    await coordinator.refresh_if_due(reference=_T0, trading_date=_DATE)
    coordinator.configure(max_age=None)  # last consumer released
    assert (
        await coordinator.refresh_if_due(reference=_T0 + timedelta(minutes=1), trading_date=_DATE)
        is False
    )
    assert len(service.calls) == 1


async def test_full_universe_is_one_refresh_call() -> None:
    service = _RecordingRefreshService()
    symbols = tuple(f"SYM{i:03d}" for i in range(208))
    coordinator = _coordinator(service, symbols=symbols)
    coordinator.configure(max_age=_5S)
    await coordinator.refresh_if_due(reference=_T0, trading_date=_DATE)
    assert len(service.calls) == 1
    assert len(service.calls[0][0]) == 208


async def test_concurrent_due_calls_coalesce_to_one_refresh() -> None:
    service = _RecordingRefreshService()
    coordinator = _coordinator(service)
    coordinator.configure(max_age=_5S)
    results = await asyncio.gather(
        coordinator.refresh_if_due(reference=_T0, trading_date=_DATE),
        coordinator.refresh_if_due(reference=_T0, trading_date=_DATE),
    )
    assert sorted(results) == [False, True]  # exactly one performed
    assert len(service.calls) == 1


async def test_provider_failure_propagates_and_consumes_the_cadence_slot() -> None:
    service = _RecordingRefreshService(error=ProviderUnavailableError())
    coordinator = _coordinator(service)
    coordinator.configure(max_age=_5S)
    with pytest.raises(ProviderUnavailableError):
        await coordinator.refresh_if_due(reference=_T0, trading_date=_DATE)
    # attempt consumed: not due again until the cadence elapses
    assert await coordinator.refresh_if_due(reference=_T0 + _3S, trading_date=_DATE) is False


async def test_cancellation_restores_the_cadence_slot_for_retry() -> None:
    service = _RecordingRefreshService(error=asyncio.CancelledError())
    coordinator = _coordinator(service)
    coordinator.configure(max_age=_5S)
    with pytest.raises(asyncio.CancelledError):
        await coordinator.refresh_if_due(reference=_T0, trading_date=_DATE)
    # slot restored: an immediate retry is due again
    service.error = None
    assert await coordinator.refresh_if_due(reference=_T0, trading_date=_DATE) is True


# --------------------------------------------------------------------------- #
# Manager coordinator → control wiring (lifecycle union drives configure)
# --------------------------------------------------------------------------- #
@dataclass
class _RecordingControl:
    configured: list[timedelta | None] = field(default_factory=list)

    def configure(self, *, max_age: timedelta | None) -> None:
        self.configured.append(max_age)


class _NoopSink:
    def set_required_timeframes(self, timeframes: frozenset[Timeframe]) -> None:
        pass


class _NoopWarmup:
    async def warmup(self, instruments, effective_requirements, *, reference):  # noqa: ANN001, ANN201
        return {}


def _requirements(max_age: timedelta | None) -> StrategyRequirements:
    freshness = (
        (FactFreshnessRequirement(fact=FactNeed.SESSION_STATISTICS, max_age=max_age),)
        if max_age is not None
        else ()
    )
    fact_needs = (FactNeed.SESSION_STATISTICS,) if max_age is not None else ()
    return StrategyRequirements(
        trigger=StrategyTrigger.ON_TICK,
        candle_completeness=CandleCompleteness.PARTIAL_ALLOWED,
        fact_needs=fact_needs,
        freshness=freshness,
    )


def _requirements_coordinator(
    control: _RecordingControl,
) -> RequirementsCoordinator:
    return RequirementsCoordinator(
        instruments=(_instrument(),),
        historical=HistoricalRequirementRegistry(),
        live=LiveTimeframeRequirementRegistry(),
        sink=_NoopSink(),
        warmup=_NoopWarmup(),
        refresh_control=control,
    )


def test_control_is_a_session_statistics_refresh_control() -> None:
    coordinator = SessionStatisticsRefreshCoordinator(
        service=_RecordingRefreshService(),  # type: ignore[arg-type]
        instruments=[_instrument()],
    )
    assert isinstance(coordinator, SessionStatisticsRefreshControl)


def test_register_configures_control_with_effective_demand() -> None:
    control = _RecordingControl()
    coordinator = _requirements_coordinator(control)
    coordinator.register("a", _requirements(_5S))
    coordinator.register("b", _requirements(_3S))
    assert control.configured[-1] == _3S  # strictest wins


def test_release_relaxes_then_deactivates() -> None:
    control = _RecordingControl()
    coordinator = _requirements_coordinator(control)
    coordinator.register("a", _requirements(_5S))
    coordinator.register("b", _requirements(_3S))
    coordinator.release("b")
    assert control.configured[-1] == _5S  # relaxes to the remaining consumer
    coordinator.release("a")
    assert control.configured[-1] is None  # last consumer released → inactive


def test_non_consumer_registration_keeps_control_inactive() -> None:
    control = _RecordingControl()
    coordinator = _requirements_coordinator(control)
    coordinator.register("a", _requirements(None))  # declares no session statistics
    assert control.configured[-1] is None
