"""Manager-side requirement orchestration bridge (P5.4; ADR-007 D3/D5/D8).

Ties a strategy's declared :class:`StrategyRequirements` to the Phase-4 historical
requirement registry, the manager-owned live-timeframe registry, the additive
live-timeframe seam, and the historical warmup port — over a fixed instrument
universe. It registers/releases per-strategy requirements, recomputes the effective
unions, applies the effective live union to the Market Engine, and warms historical
requirements, reporting whether a strategy's historical dependencies are ready.

It never touches the lifecycle FSM, the strategy implementations, the registry, or
the CandleEngine directly (docs/07 §6; ADR-007 D8): it speaks only to the two
registries and the two capability ports.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from app.market_engine.historical.requirements import HistoricalRequirementRegistry
from app.schemas.market_data import Instrument
from app.strategies.enums import FactNeed
from app.strategies.requirements import StrategyRequirements
from app.strategy_manager.fact_requirements import FactRequirementRegistry
from app.strategy_manager.live_timeframes import LiveTimeframeRequirementRegistry
from app.strategy_manager.ports import (
    HistoricalWarmupPort,
    LiveTimeframeSink,
    SessionStatisticsRefreshControl,
)


class RequirementsCoordinator:
    """Owns the requirement registries and applies effective unions to the Market Engine."""

    def __init__(
        self,
        *,
        instruments: Iterable[Instrument],
        historical: HistoricalRequirementRegistry,
        live: LiveTimeframeRequirementRegistry,
        sink: LiveTimeframeSink,
        warmup: HistoricalWarmupPort,
        fact_requirements: FactRequirementRegistry | None = None,
        refresh_control: SessionStatisticsRefreshControl | None = None,
    ) -> None:
        """Wire the coordinator to the universe, the registries, and the capability ports.

        Args:
            instruments: The instrument universe historical warmup covers.
            historical: The Phase-4 historical requirement registry (shared).
            live: The manager-owned live-timeframe requirement registry.
            sink: The additive Market-Engine live-timeframe seam.
            warmup: The historical warmup port.
            fact_requirements: The session-statistics demand registry (a fresh one is
                created when omitted).
            refresh_control: Optional session-statistics refresh-control port (ADR-009);
                when present it is (re)configured with the effective demand on every
                register/release. Absent → no session-statistics activation occurs.
        """
        self._instruments = tuple(instruments)
        self._historical = historical
        self._live = live
        self._sink = sink
        self._warmup = warmup
        self._facts = (
            fact_requirements if fact_requirements is not None else FactRequirementRegistry()
        )
        self._refresh_control = refresh_control

    def register(self, strategy_id: str, requirements: StrategyRequirements) -> None:
        """Register a strategy's historical, live, and session-statistics requirements.

        Idempotent per consumer: re-registering the same strategy replaces its
        entry rather than creating a duplicate consumer (so an ERROR→START restart
        never double-registers — ADR-007 D7/§27).
        """
        self._historical.register(strategy_id, requirements.historical)
        self._live.register(strategy_id, requirements.live_timeframes)
        self._facts.register(
            strategy_id,
            session_statistics_max_age=_session_statistics_max_age(requirements),
        )
        self._apply_session_statistics()

    def release(self, strategy_id: str) -> None:
        """Deregister a strategy's requirements and re-apply the shrunk effective unions.

        Shared requirements survive: the effective live union and session-statistics
        demand are recomputed over the remaining consumers, so anything another strategy
        still needs stays active (ADR-007 D5).
        """
        self._historical.deregister(strategy_id)
        self._live.deregister(strategy_id)
        self._facts.deregister(strategy_id)
        self.apply_live_union()
        self._apply_session_statistics()

    def _apply_session_statistics(self) -> None:
        """Push the strictest effective session-statistics demand to the refresh control."""
        if self._refresh_control is not None:
            self._refresh_control.configure(
                max_age=self._facts.effective_session_statistics_max_age()
            )

    def apply_live_union(self) -> None:
        """Push the current effective live-timeframe union to the Market-Engine seam."""
        self._sink.set_required_timeframes(self._live.effective_timeframes())

    async def warm(self, requirements: StrategyRequirements, *, reference: datetime) -> bool:
        """Warm effective historical requirements and report strategy-lifecycle readiness.

        Readiness here is *infrastructure-level* (ADR-007 partial-universe addendum
        PUR2/PUR3): the strategy is ready to RUN once the historical warmup mechanism
        executes without a *global* failure — NOT when every instrument is satisfied.
        Warmup installs an authoritative ``HistoricalContext`` per *satisfied* instrument;
        an unsatisfied one is left with none and is skipped *per-context* at evaluation
        time (``assess_readiness`` -> ``MISSING_HISTORICAL``), so a single un-warmable
        instrument never suppresses the strategy — the scanner reports ``PARTIAL``.

        Zero effective historical requirements (a live-only strategy) means no warmup
        call. *Global* failures — ``OutsideCalendarCoverageError``,
        ``HistoricalWarmupUnavailableError``, and any error warmup does not catch
        per-instrument — propagate out and fail START closed (``StrategyManager.start``
        marks ``ERROR``); they are never swallowed to keep the strategy RUNNING.
        Per-instrument source/quality failures are caught inside warmup and simply
        leave that instrument unsatisfied.

        Args:
            requirements: The starting strategy's declared requirements.
            reference: The deterministic reference instant (UTC, tz-aware).

        Returns:
            ``True`` once the warmup mechanism executed (global failures raise instead).
        """
        effective = self._historical.effective_requirements()
        if not effective:
            return True
        required = frozenset(req.timeframe for req in requirements.historical)
        if not required:
            return True
        await self._warmup.warmup(self._instruments, effective, reference=reference)
        return True


def _session_statistics_max_age(requirements: StrategyRequirements) -> timedelta | None:
    """Return the strategy's SESSION_STATISTICS max age, or None if it does not activate it.

    A strategy activates session-statistics demand only when it declares the fact *and* a
    freshness bound for it; declaring the fact without a bound is never ready (readiness
    fails closed) and does not drive the refresh cadence.
    """
    if FactNeed.SESSION_STATISTICS not in requirements.fact_needs:
        return None
    for entry in requirements.freshness:
        if entry.fact is FactNeed.SESSION_STATISTICS:
            return entry.max_age
    return None
