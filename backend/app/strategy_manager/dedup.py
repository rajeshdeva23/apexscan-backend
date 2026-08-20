"""Emission-policy dedup for published strategy results (P5.5; ADR-007 D10).

A 208-instrument scanner must not republish an identical match on every tick, so a
``StrategyResult`` is emitted only when the strategy's :class:`EmissionPolicy` says
it is *material* (ADR-007 D10). This layer is the manager-side dedup state keyed by
``(strategy_id, instrument, trading_date, emission-policy semantic key)``:

- ``CONTINUOUS`` — emit on a typed material-content change; suppress unchanged
  repeats. Material change is defined over ``status``, ``score``, ``confidence``,
  ``reason_codes``, ``metrics`` — never formatted reason text, and never the
  ``context_version`` alone (D10).
- ``EDGE_TRIGGERED`` — emit on a status transition against an implicit ``NO_MATCH``
  session baseline (``NO_MATCH → MATCHED`` and, by framework default, the falling
  ``MATCHED → NO_MATCH``); suppress same-status repeats.
- ``ONE_SHOT_PER_SESSION`` — emit the first qualifying ``MATCHED`` per instrument and
  trading date; suppress everything after it that session.

State is bounded to one small entry per ``(strategy_id, instrument)`` and is reset
in place when the ``trading_date`` rolls (a new session). Retention across the
lifecycle follows the requirement-retention rules (ADR-007 D4/D5/D6/D7): PAUSE and
ERROR retain the state, STOP and FORCE STOP reset it via :meth:`reset_strategy`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.schemas.market_data import Instrument
from app.strategies.enums import EmissionPolicy, EvaluationStatus
from app.strategies.results import MetricEntry, StrategyResult

_MaterialKey = tuple[
    EvaluationStatus,
    Decimal | None,
    Decimal | None,
    tuple[str, ...],
    tuple[MetricEntry, ...],
]


def _material_key(result: StrategyResult) -> _MaterialKey:
    """Project a result onto the typed fields that define a material change (D10)."""
    return (result.status, result.score, result.confidence, result.reason_codes, result.metrics)


@dataclass(slots=True)
class _EntryState:
    """Bounded per-``(strategy_id, instrument)`` emission state for one trading date.

    Only the field relevant to the strategy's policy is consulted; the others stay at
    their session baseline. A new ``trading_date`` replaces the whole entry.
    """

    trading_date: date | None
    last_material_key: _MaterialKey | None = None
    last_status: EvaluationStatus = EvaluationStatus.NO_MATCH
    one_shot_emitted: bool = False


class EmissionDeduplicator:
    """Manager-owned, bounded emission-policy dedup state (ADR-007 D10)."""

    def __init__(self) -> None:
        """Create an empty deduplicator tracking no strategies."""
        self._entries: dict[tuple[str, Instrument], _EntryState] = {}

    def should_emit(
        self,
        result: StrategyResult,
        *,
        policy: EmissionPolicy,
        trading_date: date | None,
    ) -> bool:
        """Return whether ``result`` is material to emit under ``policy`` this cycle.

        Mutates the bounded per-``(strategy_id, instrument)`` state so a subsequent
        unchanged observation is suppressed. A ``trading_date`` different from the
        tracked one resets the entry first (a new session — one-shot/edge/continuous
        state does not survive a session boundary, D10).

        Args:
            result: The promoted, immutable result being considered for emission.
            policy: The producing strategy's declared emission policy.
            trading_date: The exchange-local trading date of the evaluated context,
                or ``None`` when the context carries no session.

        Returns:
            Whether the result should be emitted (and downstream published).
        """
        key = (result.strategy_id, result.instrument)
        entry = self._entries.get(key)
        if entry is None or entry.trading_date != trading_date:
            entry = _EntryState(trading_date=trading_date)
            self._entries[key] = entry
        if policy is EmissionPolicy.CONTINUOUS:
            return self._continuous(entry, result)
        if policy is EmissionPolicy.EDGE_TRIGGERED:
            return self._edge(entry, result)
        return self._one_shot(entry, result)

    def reset_strategy(self, strategy_id: str) -> None:
        """Drop all emission state for a strategy (ADR-007 D5/D6 STOP / FORCE STOP)."""
        self._entries = {
            key: state for key, state in self._entries.items() if key[0] != strategy_id
        }

    def size(self) -> int:
        """Return the number of tracked ``(strategy_id, instrument)`` entries (bounded)."""
        return len(self._entries)

    @staticmethod
    def _continuous(entry: _EntryState, result: StrategyResult) -> bool:
        material = _material_key(result)
        if entry.last_material_key is not None and entry.last_material_key == material:
            return False
        entry.last_material_key = material
        return True

    @staticmethod
    def _edge(entry: _EntryState, result: StrategyResult) -> bool:
        if result.status == entry.last_status:
            return False
        entry.last_status = result.status
        return True

    @staticmethod
    def _one_shot(entry: _EntryState, result: StrategyResult) -> bool:
        if result.status is EvaluationStatus.MATCHED and not entry.one_shot_emitted:
            entry.one_shot_emitted = True
            return True
        return False
