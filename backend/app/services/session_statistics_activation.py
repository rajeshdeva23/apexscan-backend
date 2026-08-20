"""Session-statistics refresh activation coordinator (P4.6E5; ADR-009 D6/D10).

Implements the manager's :class:`SessionStatisticsRefreshControl` port and adds a
deterministic, driven :meth:`refresh_if_due` that a composition scheduler calls. The
Strategy Manager's requirement layer calls :meth:`configure` with the strictest effective
``max_age`` (or ``None`` to deactivate); the refresh cadence is that ``max_age`` (§10).
There is **no** autonomous background loop — a caller drives cadence with an explicit
reference instant, so behaviour is replay-friendly and testable without wall-clock passage.

It owns its configured canonical instrument universe (composition-provided) and delegates
one logical batch fetch+staging to the E4 :class:`SessionStatisticsRefreshService`. It
never enables authority, decides consumer freshness, mutates MarketContext, or touches a
provider directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from app.schemas.market_data import Instrument
from app.services.session_statistics_refresh import SessionStatisticsRefreshService


class SessionStatisticsRefreshCoordinator:
    """A driven refresh coordinator that activates/paces session-statistics refresh."""

    def __init__(
        self,
        *,
        service: SessionStatisticsRefreshService,
        instruments: Sequence[Instrument],
    ) -> None:
        """Wire the coordinator to the E4 refresh service and its canonical universe.

        Args:
            service: The E4 refresh/staging service driven each due cycle.
            instruments: The composition-provided canonical instrument universe.
        """
        self._service = service
        self._instruments = tuple(instruments)
        self._max_age: timedelta | None = None
        self._last_reference: datetime | None = None
        self._lock = asyncio.Lock()

    def configure(self, *, max_age: timedelta | None) -> None:
        """Set the effective refresh demand (``None`` deactivates); the cadence is ``max_age``."""
        self._max_age = max_age

    async def refresh_if_due(self, *, reference: datetime, trading_date: date) -> bool:
        """Refresh when active and the cadence has elapsed; otherwise a no-op.

        Concurrent calls are coalesced to a single underlying refresh via an in-flight lock
        plus a re-check under it. A refresh advances the cadence slot even on a provider
        failure (which propagates); a cancellation restores the slot so a later call can
        retry, and the exception propagates.

        Args:
            reference: The deterministic instant this refresh cycle is evaluated at (UTC).
            trading_date: The caller-resolved canonical exchange trading date.

        Returns:
            Whether a refresh was performed this call.
        """
        if not self._is_due(reference):
            return False
        async with self._lock:
            if not self._is_due(reference):  # re-check under the lock — coalesce concurrent calls
                return False
            previous = self._last_reference
            self._last_reference = reference  # advance the cadence slot (kept on provider failure)
            try:
                await self._service.refresh(self._instruments, trading_date=trading_date)
            except asyncio.CancelledError:
                self._last_reference = previous  # cancellation does not consume the cadence slot
                raise
            return True

    def _is_due(self, reference: datetime) -> bool:
        if self._max_age is None:
            return False  # inactive: no consumer requires session statistics
        if self._last_reference is None:
            return True  # first cycle since activation
        return reference - self._last_reference >= self._max_age
