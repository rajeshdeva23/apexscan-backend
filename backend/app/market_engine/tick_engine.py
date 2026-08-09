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

from app.events.bus import EventBus
from app.market_engine.candle_engine import CandleEngine
from app.market_engine.clock import Clock, SystemClock
from app.market_engine.context import MarketContext, SessionContext, TimeframeCandles
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.sequence import MonotonicSequence, SequenceGenerator
from app.market_engine.session import MarketSessionClassifier
from app.market_engine.state import InstrumentState, InstrumentStateRegistry
from app.market_engine.validation import ValidationOutcome, classify
from app.schemas.market_data import FeedContinuityEvent, Quote, Tick

logger = logging.getLogger(__name__)


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
        """
        self._registry = registry
        self._bus = bus
        self._clock: Clock = clock or SystemClock()
        self._sequence: SequenceGenerator = sequence or MonotonicSequence()
        self._session = session
        self._candles = candles
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

    def process(self, event: Tick | Quote) -> ProcessResult:
        """Validate and route one canonical event, updating state only if accepted.

        Args:
            event: The canonical tick or quote to process.

        Returns:
            A :class:`ProcessResult` carrying the outcome and any new context.
        """
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

    def _accept(self, event: Tick | Quote) -> ProcessResult:
        """Apply an accepted event: build the next context, publish, and store state."""
        state = self._registry.ensure(event.instrument)
        tick, quote = _merge(event, state)
        sequence = self._sequence.next_value()
        observed_at = self._clock.now()
        session = self._session_for(event)
        candle_sets = self._candle_sets_for(event, session)
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
            )
            self._bus.publish(
                MarketContextUpdated(context=context, previous_version=state.context.version)
            )
        state.latest_tick = tick
        state.latest_quote = quote
        state.last_event_timestamp = event.event_timestamp
        state.last_sequence = sequence
        state.context = context
        return ProcessResult(outcome=ValidationOutcome.ACCEPT, context=context)
