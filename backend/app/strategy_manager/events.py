"""Strategy Engine result-publication events (P5.5; docs/07 §17, ADR-007 D10/D11).

The Strategy Manager signals a completed evaluation cycle by publishing an immutable
event on the shared in-process bus (docs/07 §17.7; it never calls consumers
directly). ``StrategyResultsPublished`` carries the cycle's *emitted* (material,
deduplicated) results for one instrument at one MarketContext version, plus the
separate :class:`RankedStrategyResult` ordering projection (ADR-007 D11 — rank is
not part of the result). Ordering is per instrument, in context-version order
(docs/07 §17.3); there is no global cross-instrument ordering guarantee. The event
is only published when at least one result is material this cycle (D10).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.events.bus import Event
from app.schemas.market_data import Instrument
from app.strategies.results import StrategyResult
from app.strategy_manager.ranking import RankedStrategyResult


@dataclass(frozen=True, slots=True)
class StrategyResultsPublished(Event):
    """A completed evaluation cycle's material results for one instrument version.

    Attributes:
        instrument: The instrument whose context version was evaluated.
        context_version: The exact MarketContext version the results interpret.
        results: The emitted, deduplicated results this cycle (may include a
            ``NO_MATCH`` transition signal); each carries its own strategy and
            context versions (docs/07 §17.4).
        ranked: The deterministic presentation ordering over the emitted matches
            (docs/07 §14); a rank-free result never gains a rank here.
        trading_date: The exchange-local trading date of the evaluated session,
            taken verbatim from the MarketContext session (``None`` when the context
            carries no session). It is the authoritative snapshot-identity input for
            the cross-instrument scanner (ADR-012 NCRS9) — never derived from a clock.
    """

    instrument: Instrument
    context_version: int
    results: tuple[StrategyResult, ...]
    ranked: tuple[RankedStrategyResult, ...]
    trading_date: date | None
