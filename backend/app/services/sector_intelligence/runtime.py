"""Passive sector shadow runtime: observer + periodic evaluator (SECTOR-VIEW-1B).

Reuses the SECTOR-2 membership resolver and the SECTOR-3/4 pure math verbatim — it re-implements
no metric. The observer maintains bounded latest-observation state on the synchronous bus; a
single periodic evaluator captures a coherent copy of that state (synchronously, so no callback
interleaves on the single-threaded event loop) and produces an internal ``SectorShadowSnapshot``.
No provider I/O, no persistence, no network, no public API.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from app.events.bus import EventBus
from app.market_engine.clock import Clock, SystemClock
from app.market_intelligence.sector.membership import MembershipResolver
from app.market_intelligence.sector.metrics import (
    ConstituentObservation,
    calculate_sector_metrics,
    calculate_universe_proxy,
)
from app.market_intelligence.sector.participation import rank_sector_constituents
from app.services.sector_intelligence.config import ShadowRuntimeConfig
from app.services.sector_intelligence.diagnostics import ShadowDiagnostics, ShadowDiagnosticsView
from app.services.sector_intelligence.observer import SectorShadowObserver
from app.services.sector_intelligence.snapshot import SectorShadowSnapshot
from app.services.sector_intelligence.state import LatestObservation, ObservationState

logger = logging.getLogger(__name__)


def _ms(delta_seconds: float) -> float:
    """Convert seconds to milliseconds."""
    return delta_seconds * 1000.0


class SectorShadowRuntime:
    """Owns the passive observer and the single periodic shadow evaluator."""

    def __init__(
        self,
        *,
        bus: EventBus,
        resolver: MembershipResolver,
        config: ShadowRuntimeConfig,
        clock: Clock | None = None,
        evaluation_hook: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Compose the runtime over the shared bus and the SECTOR-2 membership resolver.

        Args:
            bus: The shared in-process EventBus carrying MarketContext lifecycle events.
            resolver: The SECTOR-2 membership resolver defining the expected universe.
            config: Validated shadow cadence + un-calibrated calculation policy.
            clock: Injected UTC clock (evaluation timestamp + duration); defaults to system.
            evaluation_hook: Optional awaitable invoked mid-evaluation. A test/instrumentation
                seam only: it lets a test hold the evaluation open to exercise the non-overlap
                guard; it is never wired in production.
        """
        self._bus = bus
        self._resolver = resolver
        self._config = config
        self._clock: Clock = clock or SystemClock()
        self._evaluation_hook = evaluation_hook
        expected = frozenset(
            identity
            for sector_id in resolver.all_primary_sectors()
            for identity in resolver.members_of_primary_sector(sector_id)
        )
        self._expected_universe_count = len(expected)
        self._diag = ShadowDiagnostics()
        self._state = ObservationState(expected)
        self._observer = SectorShadowObserver(bus=bus, state=self._state, diagnostics=self._diag)
        self._latest_snapshot: SectorShadowSnapshot | None = None
        self._evaluating = False

    def subscribe(self) -> None:
        """Attach the passive observer to the bus (idempotent)."""
        self._observer.subscribe()

    def unsubscribe(self) -> None:
        """Detach the passive observer from the bus (idempotent)."""
        self._observer.unsubscribe()

    async def run(self) -> None:
        """Evaluate once immediately, then on the configured cadence until cancelled."""
        while True:
            try:
                await self.evaluate_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # an evaluation fault must never end the driver
                logger.warning("sector shadow evaluator tick failed", exc_info=True)
            await asyncio.sleep(self._config.interval_seconds)

    async def evaluate_once(self) -> SectorShadowSnapshot | None:
        """Run one shadow evaluation, guarding against overlap and preserving the last good one.

        A concurrent entry (evaluation slower than cadence) does not start a second calculation:
        it increments ``evaluation_overruns`` and returns the current snapshot. A failing
        evaluation never replaces the last successful snapshot.
        """
        if self._evaluating:
            self._diag.evaluation_overruns += 1
            return self._latest_snapshot
        self._evaluating = True
        self._diag.snapshot_attempts += 1
        started = self._clock.now()
        try:
            observations = self._state.coherent_copy()  # atomic: no await before this point
            evaluation_time = self._clock.now()
            if self._evaluation_hook is not None:
                await self._evaluation_hook()
            snapshot = self._build_snapshot(observations, evaluation_time)
            self._latest_snapshot = snapshot
            self._diag.snapshot_successes += 1
            self._diag.last_success_timestamp = evaluation_time.isoformat()
            return snapshot
        except Exception:
            self._diag.snapshot_failures += 1
            logger.warning(
                "sector shadow evaluation failed; last-good snapshot preserved", exc_info=True
            )
            return self._latest_snapshot
        finally:
            self._diag.last_evaluation_duration_ms = _ms(
                (self._clock.now() - started).total_seconds()
            )
            self._evaluating = False

    def latest_snapshot(self) -> SectorShadowSnapshot | None:
        """Return the latest successful immutable shadow snapshot (or ``None`` before any)."""
        return self._latest_snapshot

    def diagnostics(self) -> ShadowDiagnosticsView:
        """Return an immutable copy of the bounded runtime diagnostics."""
        return self._diag.view()

    def _build_snapshot(
        self, observations: tuple[LatestObservation, ...], evaluation_time: datetime
    ) -> SectorShadowSnapshot:
        """Build one immutable shadow snapshot from a coherent state copy (pure math)."""
        trading_date = self._state.trading_date
        # Sort by identity so a given final state always yields the same snapshot regardless of
        # the order events arrived in (SECTOR-3 preserves observation order in its output).
        complete = sorted(
            (o for o in observations if o.is_complete and o.trading_date == trading_date),
            key=lambda observation: observation.identity,
        )
        by_sector: dict[str, list[ConstituentObservation]] = {}
        all_constituents: list[ConstituentObservation] = []
        if trading_date is not None:
            for observation in complete:
                sector_id = self._resolver.resolve_primary(observation.identity, on=trading_date)
                if sector_id is None:
                    continue
                constituent = ConstituentObservation(
                    identity=observation.identity,
                    sector_id=sector_id,
                    trading_date=trading_date,
                    observation_timestamp=observation.observation_timestamp,
                    last_price=observation.last_price,
                    previous_close=observation.previous_close,
                    session_open=observation.session_open,
                )
                by_sector.setdefault(sector_id, []).append(constituent)
                all_constituents.append(constituent)

        policy = self._config.policy
        proxy = (
            calculate_universe_proxy(all_constituents, policy, trading_date, evaluation_time)
            if trading_date is not None
            else None
        )
        sector_metrics = []
        stock_rankings = []
        if trading_date is not None:
            for sector_id in self._resolver.all_primary_sectors():
                expected = frozenset(
                    self._resolver.members_of_primary_sector(sector_id, on=trading_date)
                )
                sector_obs = by_sector.get(sector_id, [])
                metrics = calculate_sector_metrics(
                    sector_id, expected, sector_obs, policy, trading_date, evaluation_time, proxy
                )
                sector_metrics.append(metrics)
                stock_rankings.append(rank_sector_constituents(metrics, sector_obs, policy))

        fresh_count = proxy.valid_count if proxy is not None else 0
        return SectorShadowSnapshot(
            trading_date=trading_date,
            evaluation_timestamp=evaluation_time,
            expected_universe_count=self._expected_universe_count,
            observed_count=len(observations),
            complete_count=len(complete),
            fresh_count=fresh_count,
            stale_count=len(complete) - fresh_count,
            missing_previous_close_count=sum(1 for o in observations if o.previous_close is None),
            missing_session_open_count=sum(1 for o in observations if o.session_open is None),
            missing_last_price_count=sum(1 for o in observations if o.last_price is None),
            other_incomplete_count=_other_incomplete(observations, trading_date),
            universe_proxy=proxy,
            sector_metrics=tuple(sector_metrics),
            stock_rankings=tuple(stock_rankings),
            runtime_diagnostics=self._diag.view(),
        )


def _other_incomplete(observations: tuple[LatestObservation, ...], trading_date: object) -> int:
    """Count observations with all three prices present but not usable for this session.

    These are incomplete for a reason other than a missing price field — a missing trading
    date, or a trading date that does not match the current session (e.g. mid-rollover).
    """
    return sum(
        1
        for o in observations
        if o.last_price is not None
        and o.previous_close is not None
        and o.session_open is not None
        and not (o.trading_date is not None and o.trading_date == trading_date)
    )
