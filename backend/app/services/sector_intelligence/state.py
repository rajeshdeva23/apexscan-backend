"""Bounded latest-observation state for the sector shadow runtime (SECTOR-VIEW-1B).

One latest usable observation per resolved instrument. Cardinality never exceeds the expected
universe (unknown identities are rejected, never stored). Pure and synchronous — no asyncio, no
I/O — so the ordering rules (out-of-order, duplicate, trading-date rollover) are deterministic
and directly testable. Rollover is driven solely by ``MarketContext.session.trading_date``,
never by UTC midnight or wall-clock date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class LatestObservation:
    """Minimal immutable per-instrument values copied off a frozen MarketContext.

    Any of ``last_price`` / ``previous_close`` / ``session_open`` may be ``None`` when the live
    context does not (yet) carry it; missing values are never fabricated.
    """

    identity: str
    trading_date: date | None
    observation_timestamp: datetime
    last_price: Decimal | None
    previous_close: Decimal | None
    session_open: Decimal | None
    version: int

    @property
    def is_complete(self) -> bool:
        """True iff every SECTOR-3 ConstituentObservation input is present for a session."""
        return (
            self.trading_date is not None
            and self.last_price is not None
            and self.previous_close is not None
            and self.session_open is not None
        )


class RecordOutcome(StrEnum):
    """The result of offering one observation to the state (maps to a diagnostic counter)."""

    ACCEPTED = "accepted"
    ROLLED = "rolled"
    DUPLICATE = "duplicate"
    REJECTED_UNKNOWN = "rejected_unknown"
    REJECTED_OUT_OF_ORDER = "rejected_out_of_order"
    REJECTED_LATE_DATE = "rejected_late_date"


class ObservationState:
    """Bounded per-instrument latest-observation store with deterministic ordering rules."""

    def __init__(self, expected_identities: frozenset[str]) -> None:
        """Bind the state to the expected universe (its hard cardinality ceiling)."""
        self._expected = expected_identities
        self._latest: dict[str, LatestObservation] = {}
        self._trading_date: date | None = None

    @property
    def trading_date(self) -> date | None:
        """The session trading date the current state belongs to, or ``None`` before any."""
        return self._trading_date

    @property
    def expected_count(self) -> int:
        """The expected-universe size (state can never exceed this)."""
        return len(self._expected)

    def is_expected(self, identity: str) -> bool:
        """Whether ``identity`` belongs to the expected universe."""
        return identity in self._expected

    def __len__(self) -> int:
        """The current number of tracked instruments (<= expected count)."""
        return len(self._latest)

    def record(self, observation: LatestObservation) -> RecordOutcome:
        """Offer one observation; update state per the ordering/rollover rules.

        Rejections never mutate state. A genuine forward trading-date change clears all
        prior-session state before accepting the new day; a backward trading-date is a late
        prior-session event and is rejected. Within a session, an older timestamp is rejected
        (no rewind) and an equal timestamp is an idempotent duplicate replace.
        """
        if observation.identity not in self._expected:
            return RecordOutcome.REJECTED_UNKNOWN
        date_outcome = self._apply_trading_date(observation)
        if date_outcome is not None:
            return date_outcome
        existing = self._latest.get(observation.identity)
        if existing is not None:
            if observation.observation_timestamp < existing.observation_timestamp:
                return RecordOutcome.REJECTED_OUT_OF_ORDER
            if observation.observation_timestamp == existing.observation_timestamp:
                self._latest[observation.identity] = observation
                return RecordOutcome.DUPLICATE
        self._latest[observation.identity] = observation
        return RecordOutcome.ACCEPTED

    def _apply_trading_date(self, observation: LatestObservation) -> RecordOutcome | None:
        """Resolve the session for one observation; ``None`` means proceed to ordering checks.

        Adopts the first seen date, rolls (clearing prior-session state) on a forward change,
        and rejects a backward (late prior-session) date. A ``None`` trading date leaves the
        session unchanged (the observation is stored but will read as incomplete).
        """
        if observation.trading_date is None or self._trading_date is None:
            if observation.trading_date is not None:
                self._trading_date = observation.trading_date
            return None
        if observation.trading_date > self._trading_date:
            self._latest.clear()
            self._trading_date = observation.trading_date
            self._latest[observation.identity] = observation
            return RecordOutcome.ROLLED
        if observation.trading_date < self._trading_date:
            return RecordOutcome.REJECTED_LATE_DATE
        return None

    def coherent_copy(self) -> tuple[LatestObservation, ...]:
        """Return an immutable point-in-time snapshot of all tracked observations.

        Called synchronously (no ``await``) so no callback can interleave mid-copy on the
        single-threaded event loop — the evaluator always sees a coherent state.
        """
        return tuple(self._latest.values())
