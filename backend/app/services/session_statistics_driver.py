"""Managed session-statistics refresh driver (P4.6E7; ADR-009 addendum, ADR-010 D9).

One runtime-owned, always-running task that periodically gives the session-statistics
refresh coordinator a chance to run — but only during ``LIVE_SESSION`` (ADR-009 refresh-phase
addendum). The driver owns neither the refresh *cadence* (the coordinator decides due/not-due
from the strictest freshness ``max_age``) nor provider request pacing (the adapter's pacer);
it only supplies wake opportunities and the canonical session gate. It is broker-neutral:
no provider imports, no strategy identifiers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from app.adapters.base.errors import ProviderBoundaryError
from app.market_engine.clock import Clock
from app.market_engine.context import MarketState, SessionContext

logger = logging.getLogger(__name__)


@runtime_checkable
class DrivenSessionStatisticsRefresh(Protocol):
    """The narrow driven-refresh capability the driver invokes (the E5 coordinator)."""

    async def refresh_if_due(self, *, reference: datetime, trading_date: date) -> bool:
        """Refresh when a consumer requires it and the cadence has elapsed; else a no-op."""


@runtime_checkable
class SessionPhaseSource(Protocol):
    """The canonical session classifier the driver reads phase/trading-date from."""

    def classify(self, instant: datetime, *, halt_active: bool = False) -> SessionContext:
        """Classify a UTC instant into broker-neutral session facts."""


class SessionStatisticsRefreshDriver:
    """Drives ``refresh_if_due`` during ``LIVE_SESSION`` only (demand/cadence gated downstream)."""

    def __init__(
        self,
        *,
        refresh: DrivenSessionStatisticsRefresh,
        classifier: SessionPhaseSource,
        clock: Clock,
        poll_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Wire the driver to the shared refresh coordinator, classifier, and clock.

        Args:
            refresh: The driven refresh coordinator (owns demand + cadence).
            classifier: The same canonical classifier the TickEngine uses.
            clock: The injected UTC clock (``SystemClock`` in production).
            poll_seconds: The infrastructure wake interval (not the refresh cadence).
            sleep: The wait seam; injectable for deterministic tests.
        """
        self._refresh = refresh
        self._classifier = classifier
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._sleep = sleep

    async def run(self) -> None:
        """Loop: evaluate one gated cycle, then wait, until cancelled on shutdown."""
        while True:
            await self._cycle(self._clock.now())
            await self._sleep(self._poll_seconds)

    async def _cycle(self, reference: datetime) -> bool:
        """Give the coordinator one opportunity iff the phase is ``LIVE_SESSION``.

        Returns whether a refresh was performed. An expected provider failure is logged and
        swallowed so the driver survives to the next cycle (E4/E5 fail-closed: no fabricated
        observation, no ``as_of`` advance); ``CancelledError`` and unexpected programming
        errors propagate to the owner.
        """
        session = self._classifier.classify(reference)
        if session.market_state is not MarketState.LIVE_SESSION:
            return False
        try:
            return await self._refresh.refresh_if_due(
                reference=reference, trading_date=session.trading_date
            )
        except ProviderBoundaryError as error:
            logger.warning("session-statistics refresh cycle failed: %s", type(error).__name__)
            return False
