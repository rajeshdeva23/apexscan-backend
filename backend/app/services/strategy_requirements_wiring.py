"""Composition-layer adapters binding manager ports to the Market Engine (P5.4).

The Strategy Manager depends only on the capability ports in
``app.strategy_manager.ports``; it never imports the ``CandleEngine`` or the
``HistoricalWarmupService`` (P5.0 import boundary). This module — in the composition
layer, outside the guarded manager package — supplies the concrete adapters and a
factory that assembles a :class:`RequirementsCoordinator` over the real Market
Engine. The adapters translate only: they add no policy.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from app.market_engine.candle_engine import CandleEngine
from app.market_engine.historical.cache import HistoricalCache
from app.market_engine.historical.calendar_window import (
    CalendarCoverage,
    HistoricalCalendarWindow,
)
from app.market_engine.historical.coordinator import HistoricalCoordinator
from app.market_engine.historical.requirements import (
    HistoricalRequirement,
    HistoricalRequirementRegistry,
)
from app.market_engine.historical.service import HistoricalRangePlanner, HistoricalWarmupService
from app.market_engine.historical.source import HistoricalSource
from app.market_engine.session import (
    SessionSchedule,
    TradingCalendar,
    TradingSessionOverride,
)
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument
from app.strategy_manager.fact_requirements import FactRequirementRegistry
from app.strategy_manager.live_timeframes import LiveTimeframeRequirementRegistry
from app.strategy_manager.ports import HistoricalWarmupPort, SessionStatisticsRefreshControl
from app.strategy_manager.requirements_bridge import RequirementsCoordinator

_DEFAULT_HISTORICAL_CONCURRENCY = 8


class HistoricalWarmupUnavailableError(RuntimeError):
    """Raised if a strategy needs historical warmup but no calendar coverage was configured."""


class UnavailableHistoricalWarmup:
    """A fail-closed :class:`HistoricalWarmupPort` for runtimes with no historical coverage.

    Lets the fact/live requirement seams compose without an (ungoverned) historical calendar
    coverage when there are zero historical consumers. The coordinator only invokes this when
    a strategy actually declares historical requirements — in which case it fails closed,
    because history cannot be warmed without a governed coverage window.
    """

    async def warmup(
        self,
        instruments: Sequence[Instrument],
        effective_requirements: Sequence[HistoricalRequirement],
        *,
        reference: datetime,
    ) -> Mapping[Instrument, frozenset[Timeframe]]:
        """Fail closed: historical warmup is not configured in this runtime."""
        raise HistoricalWarmupUnavailableError(
            "historical warmup requested but no calendar coverage is configured"
        )


class CandleEngineTimeframeSink:
    """Adapts a :class:`CandleEngine` to the :class:`LiveTimeframeSink` port."""

    def __init__(self, engine: CandleEngine) -> None:
        """Wrap the candle engine whose active timeframe set is driven."""
        self._engine = engine

    def set_required_timeframes(self, timeframes: frozenset[Timeframe]) -> None:
        """Forward the effective timeframe set to the engine's additive seam."""
        self._engine.set_required_timeframes(timeframes)


class HistoricalWarmupAdapter:
    """Adapts a :class:`HistoricalWarmupService` to the :class:`HistoricalWarmupPort`."""

    def __init__(self, service: HistoricalWarmupService) -> None:
        """Wrap the warmup service whose per-instrument statuses are projected."""
        self._service = service

    async def warmup(
        self,
        instruments: Sequence[Instrument],
        effective_requirements: Sequence[HistoricalRequirement],
        *,
        reference: datetime,
    ) -> Mapping[Instrument, frozenset[Timeframe]]:
        """Warm the requirements and project each status to its satisfied timeframes."""
        statuses = await self._service.warmup(
            instruments, effective_requirements, reference=reference
        )
        return {instrument: frozenset(status.satisfied) for instrument, status in statuses.items()}


def build_historical_warmup_service(
    *,
    source: HistoricalSource,
    registry: InstrumentStateRegistry,
    schedule: SessionSchedule,
    exchange_timezone: str,
    calendar: TradingCalendar,
    coverage: CalendarCoverage,
    candles: CandleEngine | None = None,
    max_concurrency: int = _DEFAULT_HISTORICAL_CONCURRENCY,
    overrides: Iterable[TradingSessionOverride] = (),
) -> HistoricalWarmupService:
    """Assemble the Phase-4 historical warmup stack over a broker-neutral source.

    Pure construction — no provider I/O until :meth:`HistoricalWarmupService.warmup`
    runs on a strategy START. ``supports_current_day`` stays ``False`` (ADR-009 /
    docs/06): current-day intervals remain withheld.

    Args:
        source: The broker-neutral historical source (e.g. from ``broker_historical_source``).
        registry: The shared per-instrument state registry (same one the TickEngine uses).
        schedule: The exchange session schedule.
        exchange_timezone: The IANA exchange timezone.
        calendar: The trading calendar.
        coverage: The authoritative calendar coverage window.
        candles: The shared candle engine for reconciliation (no-op when omitted).
        max_concurrency: Bounded historical fetch concurrency.
        overrides: Per-date session-hour overrides for exceptional OPEN sessions;
            empty (the production default) preserves the ordinary schedule behaviour.

    Returns:
        A :class:`HistoricalWarmupService` sharing the injected registry and candle engine.
    """
    coordinator = HistoricalCoordinator(
        source=source, cache=HistoricalCache(), max_concurrency=max_concurrency
    )
    planner = HistoricalRangePlanner(
        schedule=schedule,
        exchange_timezone=exchange_timezone,
        calendar_window=HistoricalCalendarWindow(calendar=calendar, coverage=coverage),
        overrides=overrides,
    )
    return HistoricalWarmupService(
        registry=registry,
        coordinator=coordinator,
        planner=planner,
        candles=candles,
        supports_current_day=False,
    )


def build_requirements_coordinator(
    *,
    instruments: Iterable[Instrument],
    historical: HistoricalRequirementRegistry,
    engine: CandleEngine,
    warmup: HistoricalWarmupService | None = None,
    warmup_port: HistoricalWarmupPort | None = None,
    live: LiveTimeframeRequirementRegistry | None = None,
    fact_requirements: FactRequirementRegistry | None = None,
    refresh_control: SessionStatisticsRefreshControl | None = None,
) -> RequirementsCoordinator:
    """Assemble a :class:`RequirementsCoordinator` over the real Market Engine.

    Exactly one of ``warmup`` (a warmup service, wrapped into the port) or ``warmup_port``
    (an already-built port, e.g. :class:`UnavailableHistoricalWarmup` when no calendar
    coverage is configured) must be supplied.

    Args:
        instruments: The instrument universe historical warmup covers.
        historical: The shared Phase-4 historical requirement registry.
        engine: The live candle engine to drive via its additive timeframe seam.
        warmup: The Phase-4 historical warmup service (wrapped into the port).
        warmup_port: A pre-built historical warmup port (alternative to ``warmup``).
        live: An optional pre-existing live-timeframe registry (a fresh one is
            created when omitted).
        fact_requirements: An optional pre-existing fact registry (ADR-009 D11).
        refresh_control: The session-statistics refresh-control seam that fact demand
            activates (ADR-009 D11); ``None`` leaves session-statistics demand inert.

    Returns:
        A coordinator wired to the concrete Market-Engine adapters.

    Raises:
        ValueError: If neither or both of ``warmup``/``warmup_port`` are supplied.
    """
    if (warmup is None) == (warmup_port is None):
        raise ValueError("supply exactly one of warmup or warmup_port")
    port = warmup_port if warmup_port is not None else HistoricalWarmupAdapter(warmup)  # type: ignore[arg-type]
    return RequirementsCoordinator(
        instruments=instruments,
        historical=historical,
        live=live if live is not None else LiveTimeframeRequirementRegistry(),
        sink=CandleEngineTimeframeSink(engine),
        warmup=port,
        fact_requirements=fact_requirements,
        refresh_control=refresh_control,
    )
