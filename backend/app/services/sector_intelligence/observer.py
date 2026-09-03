"""Passive EventBus observer for the sector shadow runtime (SECTOR-VIEW-1B).

Subscribes only to the generic ``MarketContextCreated`` / ``MarketContextUpdated`` events. The
EventBus is synchronous and propagates subscriber exceptions into the publisher (the ingestion
task), so the callback is a strictly non-throwing O(1) boundary: it extracts identity and the
generic fields, updates bounded state and counters, and returns. It performs no sector
calculation, ranking, serialization, I/O, sleeping, or network work.
"""

from __future__ import annotations

import logging
from typing import Any

from app.events.bus import EventBus, Subscription
from app.market_engine.context import MarketContext
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_intelligence.sector.models import instrument_identity
from app.services.sector_intelligence.diagnostics import ShadowDiagnostics
from app.services.sector_intelligence.state import (
    LatestObservation,
    ObservationState,
    RecordOutcome,
)

logger = logging.getLogger(__name__)


class SectorShadowObserver:
    """Read-only bus observer that maintains bounded latest-observation state."""

    def __init__(
        self, *, bus: EventBus, state: ObservationState, diagnostics: ShadowDiagnostics
    ) -> None:
        """Wire the observer to the shared bus, the bounded state, and the counters."""
        self._bus = bus
        self._state = state
        self._diag = diagnostics
        self._subscriptions: list[Subscription[Any]] = []

    def subscribe(self) -> None:
        """Attach the read-only handlers to both context lifecycle events (idempotent)."""
        if self._subscriptions:
            return
        self._subscriptions.append(self._bus.subscribe(MarketContextCreated, self._on_context))
        self._subscriptions.append(self._bus.subscribe(MarketContextUpdated, self._on_context))

    def unsubscribe(self) -> None:
        """Detach all handlers (idempotent)."""
        for subscription in self._subscriptions:
            self._bus.unsubscribe(subscription)
        self._subscriptions.clear()

    def _on_context(self, event: MarketContextCreated | MarketContextUpdated) -> None:
        """Bus callback: never raises (the bus has no isolation), never does heavy work."""
        try:
            self._record(event.context)
        except Exception:  # a shadow fault must never break ingestion
            self._diag.events_rejected += 1
            logger.warning("sector shadow observer skipped a context", exc_info=True)

    def _record(self, context: MarketContext) -> None:
        """Extract generic fields and offer them to the bounded state (O(1))."""
        self._diag.events_received += 1
        identity = instrument_identity(context.instrument)
        if not self._state.is_expected(identity):
            self._diag.unknown_instruments += 1
            self._diag.events_rejected += 1
            return
        tick = context.latest_tick
        ohlc = tick.session_ohlc if tick is not None else None
        observation = LatestObservation(
            identity=identity,
            trading_date=context.session.trading_date if context.session is not None else None,
            observation_timestamp=context.event_timestamp,
            last_price=tick.last_price if tick is not None else None,
            previous_close=context.previous_close,
            session_open=ohlc.open_price if ohlc is not None else None,
            version=context.version,
        )
        self._apply(self._state.record(observation))

    def _apply(self, outcome: RecordOutcome) -> None:
        """Map a state outcome to the bounded diagnostic counters."""
        if outcome is RecordOutcome.ACCEPTED:
            self._diag.events_accepted += 1
        elif outcome is RecordOutcome.ROLLED:
            self._diag.events_accepted += 1
            self._diag.rollovers += 1
        elif outcome is RecordOutcome.DUPLICATE:
            self._diag.duplicate_events += 1
        elif outcome is RecordOutcome.REJECTED_OUT_OF_ORDER:
            self._diag.events_rejected += 1
            self._diag.out_of_order_events += 1
        elif outcome is RecordOutcome.REJECTED_LATE_DATE:
            self._diag.events_rejected += 1
            self._diag.late_trading_date_events += 1
        else:  # REJECTED_UNKNOWN is filtered before record(); defensive only
            self._diag.events_rejected += 1
            self._diag.unknown_instruments += 1
