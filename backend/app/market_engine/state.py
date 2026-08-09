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

from app.market_engine.context import MarketContext
from app.market_engine.historical.context import HistoricalContext
from app.schemas.market_data import Instrument, Quote, Tick


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
    """

    instrument: Instrument
    latest_tick: Tick | None = None
    latest_quote: Quote | None = None
    last_event_timestamp: datetime | None = None
    last_sequence: int | None = None
    context: MarketContext | None = None
    historical: HistoricalContext | None = None


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
