"""Deterministic tick/quote routing and MarketContext progression (docs/06 §11-§12).

Validated canonical Tick/Quote events are routed per instrument. An accepted
event advances that instrument's MarketContext by exactly one version and
publishes a lifecycle event; a rejected event (invalid, duplicate, or stale/
out-of-order) causes no state mutation, no version increment, and no publication
(docs/06 §9.2). Instruments are processed independently with no shared mutable
state (docs/06 §12.8). This slice builds no candles, sessions, historical
context, or features.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from app.events.bus import EventBus
from app.market_engine.candle_engine import CandleEngine
from app.market_engine.clock import Clock, SystemClock
from app.market_engine.context import (
    MarketContext,
    SessionContext,
    SessionStatistics,
    TimeframeCandles,
)
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.sequence import MonotonicSequence, SequenceGenerator
from app.market_engine.session import MarketSessionClassifier
from app.market_engine.session_statistics import (
    SessionStatisticsAuthority,
    resolve_session_statistics,
)
from app.market_engine.state import InstrumentState, InstrumentStateRegistry
from app.market_engine.validation import ValidationOutcome, classify
from app.schemas.market_data import (
    FeedContinuityEvent,
    MarketReference,
    Quote,
    SessionStatisticsObservation,
    Tick,
)

logger = logging.getLogger(__name__)

# Default (immutable) authority: both canonical sources (staged observation and
# tick-carried aggregate) are unverified until their own provider semantics are verified
# (P4.6D/E6), so a default-constructed engine never emits AUTHORITATIVE session statistics.
_DISABLED_SESSION_STATISTICS_AUTHORITY = SessionStatisticsAuthority()


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """The outcome of processing one canonical event.

    Attributes:
        outcome: How the event was classified.
        context: The new immutable MarketContext when accepted; None otherwise.
    """

    outcome: ValidationOutcome
    context: MarketContext | None


def _merge(event: Tick | Quote, state: InstrumentState | None) -> tuple[Tick | None, Quote | None]:
    """Merge one event onto prior state, preserving the unrelated event type.

    A tick updates the latest tick and preserves the prior quote; a quote updates
    the latest quote and preserves the prior tick (docs/06 §6.3).
    """
    prior_tick = state.latest_tick if state is not None else None
    prior_quote = state.latest_quote if state is not None else None
    if isinstance(event, Tick):
        return event, prior_quote
    return prior_tick, event


def _carried_previous_close(
    prior: MarketContext, new_session: SessionContext | None
) -> Decimal | None:
    """Carry a known previous_close forward across updates; reset on a genuine rollover.

    A later Tick/Quote whose session trading date differs from the prior context's clears
    the reference (a new session's previous close arrives via its own MarketReference); an
    event that merely omits it (every Tick/Quote does) never erases a valid same-day value.
    """
    if (
        new_session is not None
        and prior.session is not None
        and new_session.trading_date != prior.session.trading_date
    ):
        return None
    return prior.previous_close


class TickEngine:
    """Routes validated canonical events into per-instrument MarketContext versions."""

    def __init__(
        self,
        *,
        registry: InstrumentStateRegistry,
        bus: EventBus,
        clock: Clock | None = None,
        sequence: SequenceGenerator | None = None,
        session: MarketSessionClassifier | None = None,
        candles: CandleEngine | None = None,
        session_statistics_authority: SessionStatisticsAuthority = (
            _DISABLED_SESSION_STATISTICS_AUTHORITY
        ),
    ) -> None:
        """Wire the engine to its registry, bus, clock/sequence, session, and candles.

        Args:
            registry: Per-instrument state registry seeded with the known universe.
            bus: The in-process event bus for publishing context lifecycle events.
            clock: Injected UTC clock; defaults to the system clock.
            sequence: Injected sequence generator; defaults to a fresh monotonic one.
            session: Optional session classifier; when present, accepted updates are
                stamped with session facts derived from the event's timestamp.
            candles: Optional candle engine; when present, accepted ticks are
                aggregated and the resulting candle sets are stamped into the context.
            session_statistics_authority: The injected per-source capability gating
                authoritative session statistics (ADR-009 D6/D7). Defaults to **both
                sources disabled** — a valid aggregate never becomes AUTHORITATIVE until
                that source's own provider semantics are verified (P4.6D/E6); this default
                must not be enabled in production wiring in this slice.
        """
        self._registry = registry
        self._bus = bus
        self._clock: Clock = clock or SystemClock()
        self._sequence: SequenceGenerator = sequence or MonotonicSequence()
        self._session = session
        self._candles = candles
        self._session_statistics_authority = session_statistics_authority
        self._halt_active = False

    def set_halt(self, *, active: bool) -> None:
        """Record the external emergency-halt fact reflected on the next accepted update.

        The engine does not detect halts (docs/06 §7.3); a halt reported by an
        external source is stamped into the session of the next accepted event.
        """
        self._halt_active = active

    def on_feed_continuity(self, event: FeedContinuityEvent) -> None:
        """Apply a broker-neutral feed-continuity fact to candle completeness (ADR-006).

        Per ADR-006 §28 (and P4.3's no-timer-only-update decision), a continuity
        fact does not mint an event-less MarketContext version; it mutates candle
        completeness state and surfaces on the next accepted tick's context.
        """
        if self._candles is not None:
            self._candles.record_continuity(event)

    def process(self, event: Tick | Quote | MarketReference) -> ProcessResult:
        """Validate and route one canonical event, updating state only if accepted.

        Args:
            event: The canonical tick, quote, or session reference to process.

        Returns:
            A :class:`ProcessResult` carrying the outcome and any new context.
        """
        if isinstance(event, MarketReference):
            return self._accept_reference(event)
        instrument = event.instrument
        state = self._registry.get(instrument)
        outcome = classify(
            event,
            known=self._registry.is_known(instrument),
            state=state,
            now=self._clock.now(),
        )
        if outcome is not ValidationOutcome.ACCEPT:
            logger.debug(
                "rejected %s for %s: %s", type(event).__name__, instrument.symbol, outcome.value
            )
            return ProcessResult(outcome=outcome, context=None)
        return self._accept(event)

    def _session_for(self, event: Tick | Quote) -> SessionContext | None:
        """Classify session facts for an accepted event from its event time."""
        if self._session is None:
            return None
        return self._session.classify(event.event_timestamp, halt_active=self._halt_active)

    def _candle_sets_for(
        self, event: Tick | Quote, session: SessionContext | None
    ) -> tuple[TimeframeCandles, ...]:
        """Aggregate an accepted trade tick into candles and return the current sets.

        Only trade ticks update candles (docs/06 §13.2; §24) — quote-book updates
        never do. Returns the immutable per-timeframe candle snapshot to stamp.
        """
        if self._candles is None:
            return ()
        if isinstance(event, Tick) and session is not None:
            self._candles.update(event, session)
        return self._candles.candle_sets_for(event.instrument)

    def _session_statistics_for(
        self, event: Tick | Quote, session: SessionContext | None, state: InstrumentState
    ) -> tuple[SessionStatistics | None, SessionStatisticsObservation | None]:
        """Resolve this datum's session statistics and remaining staged observation (P4.6B/E2).

        An eligible staged observation (ADR-009) supplies the aggregate and is consumed,
        taking precedence over the tick-carried provisional aggregate; otherwise the tick
        aggregate (only a trade tick carries one, docs/06 §13.2) is applied. A quote-book
        update carries no aggregate. All authority/phase/reset/reconciliation/precedence
        policy lives in :func:`resolve_session_statistics` — never duplicated here.
        """
        aggregate = event.session_ohlc if isinstance(event, Tick) else None
        return resolve_session_statistics(
            aggregate=aggregate,
            aggregate_as_of=event.event_timestamp,
            staged=state.staged_session_statistics_observation,
            session=session,
            previous=state.session_statistics,
            authority=self._session_statistics_authority,
        )

    def _accept(self, event: Tick | Quote) -> ProcessResult:
        """Apply an accepted event: build the next context, publish, and store state."""
        state = self._registry.ensure(event.instrument)
        tick, quote = _merge(event, state)
        sequence = self._sequence.next_value()
        observed_at = self._clock.now()
        session = self._session_for(event)
        candle_sets = self._candle_sets_for(event, session)
        statistics, staged_after = self._session_statistics_for(event, session, state)
        if state.context is None:
            context = MarketContext.initial(
                event.instrument,
                sequence=sequence,
                event_timestamp=event.event_timestamp,
                observed_at=observed_at,
                latest_tick=tick,
                latest_quote=quote,
                candle_sets=candle_sets,
                session=session,
                historical=state.historical,
                session_statistics=statistics,
            )
            self._bus.publish(MarketContextCreated(context=context))
        else:
            context = state.context.with_update(
                sequence=sequence,
                event_timestamp=event.event_timestamp,
                observed_at=observed_at,
                latest_tick=tick,
                latest_quote=quote,
                candle_sets=candle_sets,
                session=session,
                historical=state.historical,
                session_statistics=statistics,
                previous_close=_carried_previous_close(state.context, session),
            )
            self._bus.publish(
                MarketContextUpdated(context=context, previous_version=state.context.version)
            )
        state.latest_tick = tick
        state.latest_quote = quote
        state.last_event_timestamp = event.event_timestamp
        state.last_sequence = sequence
        state.context = context
        state.session_statistics = statistics
        state.staged_session_statistics_observation = staged_after
        return ProcessResult(outcome=ValidationOutcome.ACCEPT, context=context)

    def _accept_reference(self, event: MarketReference) -> ProcessResult:
        """Apply a session reference (previous_close) onto the instrument's context.

        Unlike a Tick/Quote, a MarketReference carries no wire timestamp (the provider
        packet has none), so the engine stamps its own clock time and classifies the
        session from it. Unknown instruments fail closed. All prior observable state
        (tick, quote, candles, session statistics, historical) is preserved unchanged;
        only ``previous_close`` is set. No candle aggregation or statistics resolution
        runs — a reference is not a trade.
        """
        if not self._registry.is_known(event.instrument):
            logger.debug("rejected MarketReference for unknown %s", event.instrument.symbol)
            return ProcessResult(outcome=ValidationOutcome.INVALID, context=None)
        state = self._registry.ensure(event.instrument)
        sequence = self._sequence.next_value()
        now = self._clock.now()
        session = (
            self._session.classify(now, halt_active=self._halt_active)
            if self._session is not None
            else None
        )
        if state.context is None:
            context = MarketContext.initial(
                event.instrument,
                sequence=sequence,
                event_timestamp=now,
                observed_at=now,
                session=session,
                historical=state.historical,
                previous_close=event.previous_close,
            )
            self._bus.publish(MarketContextCreated(context=context))
        else:
            context = state.context.with_update(
                sequence=sequence,
                event_timestamp=now,
                observed_at=now,
                latest_tick=state.latest_tick,
                latest_quote=state.latest_quote,
                candle_sets=state.context.candle_sets,
                session=session if session is not None else state.context.session,
                historical=state.historical,
                session_statistics=state.session_statistics,
                previous_close=event.previous_close,
            )
            self._bus.publish(
                MarketContextUpdated(context=context, previous_version=state.context.version)
            )
        state.last_sequence = sequence
        state.context = context
        return ProcessResult(outcome=ValidationOutcome.ACCEPT, context=context)
