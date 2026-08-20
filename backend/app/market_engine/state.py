"""Per-instrument state ownership for the Market Engine (docs/06 §4.2, §12.8).

Each instrument owns isolated current state; there is no shared mutable
cross-instrument market state. State is bounded by design — only the latest
tick/quote, the last accepted event time and sequence, and the current
MarketContext are retained (no per-instrument event history). Nothing is
persisted; the registry lives in memory only (docs/02 §7).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from app.market_engine.context import MarketContext, SessionStatistics
from app.market_engine.historical.context import HistoricalContext
from app.schemas.market_data import Instrument, Quote, SessionStatisticsObservation, Tick


@dataclass(slots=True)
class InstrumentState:
    """The bounded, isolated current state for a single instrument.

    Attributes:
        instrument: The canonical instrument this state belongs to.
        latest_tick: The most recently accepted tick, if any.
        latest_quote: The most recently accepted quote, if any.
        last_event_timestamp: The event time of the most recently accepted event.
        last_sequence: The sequence value stamped on the current context.
        context: The current immutable MarketContext, if one exists yet.
        historical: The installed immutable historical snapshot, if any. Carried
            forward into each new context; installing it mints no version itself.
        session_statistics: The current authoritative session statistics, if any
            (ADR-008). The bounded per-instrument owner slot; written by the TickEngine
            on the accepted-datum path (P4.6C).
        staged_session_statistics_observation: A pending broker-neutral session-statistics
            observation awaiting the next accepted datum to surface (ADR-009 D4). At most
            one per instrument; staging mints no version and publishes no event.
    """

    instrument: Instrument
    latest_tick: Tick | None = None
    latest_quote: Quote | None = None
    last_event_timestamp: datetime | None = None
    last_sequence: int | None = None
    context: MarketContext | None = None
    historical: HistoricalContext | None = None
    session_statistics: SessionStatistics | None = None
    staged_session_statistics_observation: SessionStatisticsObservation | None = None


class InstrumentStateRegistry:
    """Owns per-instrument state for the known scanner universe (in memory only)."""

    def __init__(self, known_instruments: Iterable[Instrument]) -> None:
        """Seed the registry with the validated universe.

        Phase 3 owns universe discovery; this registry only records which
        instruments are known and holds their evolving state.

        Args:
            known_instruments: The canonical instruments the engine may process.
        """
        self._known = frozenset(known_instruments)
        self._states: dict[Instrument, InstrumentState] = {}

    def is_known(self, instrument: Instrument) -> bool:
        """Return whether the instrument belongs to the known universe."""
        return instrument in self._known

    def get(self, instrument: Instrument) -> InstrumentState | None:
        """Return the current state for an instrument, or None if none exists yet."""
        return self._states.get(instrument)

    def ensure(self, instrument: Instrument) -> InstrumentState:
        """Return the instrument's state, creating empty state on first use."""
        state = self._states.get(instrument)
        if state is None:
            state = InstrumentState(instrument=instrument)
            self._states[instrument] = state
        return state

    def install_historical(self, instrument: Instrument, historical: HistoricalContext) -> None:
        """Atomically install a historical snapshot onto one instrument's state.

        The snapshot replaces any previously installed one for that instrument and
        is isolated per instrument. Installation mutates only this instrument's
        state: it publishes no event, mints no MarketContext version, and performs
        no provider interaction — the snapshot surfaces on the next accepted datum
        via the engine's carry-forward.

        Args:
            instrument: The instrument to install the snapshot for.
            historical: The immutable historical snapshot; its instrument must
                match ``instrument``.

        Raises:
            ValueError: If the snapshot's instrument does not match ``instrument``.
        """
        if historical.instrument != instrument:
            raise ValueError("historical context instrument must match the target instrument")
        self.ensure(instrument).historical = historical

    def stage_session_statistics_observation(
        self, instrument: Instrument, observation: SessionStatisticsObservation
    ) -> None:
        """Stage a broker-neutral session-statistics observation for one instrument (ADR-009 D4).

        Bounded to one pending observation per instrument, ordered by ``observed_at``: an
        older observation is ignored (fail-closed); an identical one at the same instant is
        idempotent; a *different* one at the same instant is rejected (no silent
        last-write-wins). Staging mutates only this instrument's state — it mints no
        MarketContext version, publishes no event, and fabricates no Tick; the observation
        surfaces on the next accepted datum.

        Args:
            instrument: The instrument to stage the observation for.
            observation: The immutable observation; its instrument must match ``instrument``.

        Raises:
            ValueError: If the observation's instrument does not match ``instrument``, or a
                different observation is already staged at the same ``observed_at``.
        """
        if observation.instrument != instrument:
            raise ValueError("session statistics observation instrument must match the target")
        state = self.ensure(instrument)
        current = state.staged_session_statistics_observation
        if current is not None:
            if observation.observed_at < current.observed_at:
                return  # older observation ignored — never regress the staged one
            if observation.observed_at == current.observed_at and observation != current:
                raise ValueError("conflicting session statistics observation at the same instant")
        state.staged_session_statistics_observation = observation
