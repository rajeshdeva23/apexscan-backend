"""Exact, pure reconciliation of incomplete intervals with authoritative candles (P4.5D).

A P4.4B :class:`IncompleteCandle` is replaced by an authoritative canonical
:class:`Candle` only when their :class:`CandleIdentity` matches *exactly* — same
instrument, timeframe, and start/end timestamps (no fuzzy or overlap matching).
This module is pure: it decides the outcome and never fetches, resamples, or
mutates. The live :class:`~app.market_engine.candle_engine.CandleEngine` applies
the state transition; the historical service supplies the authoritative candles.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, auto

from app.market_engine.context import IncompleteCandle
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, Instrument


class ReconciliationOutcome(StrEnum):
    """The broker-neutral result of attempting to reconcile one interval."""

    RECONCILED = auto()
    ALREADY_RECONCILED = auto()
    NO_MATCH = auto()
    NO_AUTHORITATIVE_CANDLE = auto()
    CONFLICT = auto()
    OUT_OF_WINDOW = auto()
    CURRENT_DAY_WITHHELD = auto()


@dataclass(frozen=True, slots=True)
class CandleIdentity:
    """The exact, broker-neutral identity of one candle interval.

    Start and end timestamps fully determine the trading session and bucket, so no
    separate provider id, strategy id, or trading-date field is carried.
    """

    instrument: Instrument
    timeframe: Timeframe
    start_timestamp: datetime
    end_timestamp: datetime


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """The outcome of one reconciliation attempt, with the finalized candle if any."""

    identity: CandleIdentity
    outcome: ReconciliationOutcome
    candle: Candle | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationSummary:
    """The per-instrument collection of reconciliation results."""

    instrument: Instrument
    results: tuple[ReconciliationResult, ...]


def identity_of(candle: Candle, timeframe: Timeframe) -> CandleIdentity:
    """Return the identity of a canonical candle at a given timeframe."""
    return CandleIdentity(
        instrument=candle.instrument,
        timeframe=timeframe,
        start_timestamp=candle.start_timestamp,
        end_timestamp=candle.end_timestamp,
    )


def identity_of_incomplete(candle: IncompleteCandle) -> CandleIdentity:
    """Return the identity of an incomplete interval (carries its own timeframe)."""
    return CandleIdentity(
        instrument=candle.instrument,
        timeframe=candle.timeframe,
        start_timestamp=candle.start_timestamp,
        end_timestamp=candle.end_timestamp,
    )


def match_outcome(
    *,
    authoritative: Candle,
    timeframe: Timeframe,
    incomplete: Iterable[IncompleteCandle],
    finalized: Iterable[Candle],
) -> ReconciliationOutcome:
    """Classify how an authoritative candle relates to the current candle state.

    Args:
        authoritative: The authoritative historical candle to apply.
        timeframe: The timeframe the candle belongs to.
        incomplete: The currently-retained incomplete intervals.
        finalized: The currently-retained authoritative finalized candles.

    Returns:
        ``ALREADY_RECONCILED`` / ``CONFLICT`` if an equal / differing finalized
        candle shares the identity, ``RECONCILED`` if a matching incomplete
        interval exists, else ``NO_MATCH``.
    """
    identity = identity_of(authoritative, timeframe)
    for finalized_candle in finalized:
        if identity_of(finalized_candle, timeframe) == identity:
            if finalized_candle == authoritative:
                return ReconciliationOutcome.ALREADY_RECONCILED
            return ReconciliationOutcome.CONFLICT
    for interval in incomplete:
        if identity_of_incomplete(interval) == identity:
            return ReconciliationOutcome.RECONCILED
    return ReconciliationOutcome.NO_MATCH
