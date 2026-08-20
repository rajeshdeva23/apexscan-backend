"""Governed, broker-neutral trading-calendar dataset model (ADR-011-DATA-R1).

A :class:`TradingCalendarDataset` is a validated, version-controlled, provider-neutral
container for one exchange segment's date-level calendar authority over a bounded
coverage window: default weekday trading, default weekend closure, explicit weekday
closures, exceptional OPEN sessions, and per-date live-interval overrides for those
OPEN sessions (ADR-011 exception-model addendum M12; multi-interval addendum MI20).

Provisioned state is the *existence* of a validated dataset object; its absence
(``None``) is the unprovisioned state — never a magic boolean. Provenance lives only
here, never in the domain :class:`~app.market_engine.session.TradingCalendar`
(governance §8). The model validates fail-fast at construction and never silently
repairs, sorts, or deduplicates authoritative data (MI4).
"""

from __future__ import annotations

import re
from datetime import date, time
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.market_engine.historical.calendar_window import CalendarCoverage
from app.market_engine.session import (
    EffectiveSchedule,
    SessionSchedule,
    TradingCalendar,
    TradingInterval,
    TradingSessionOverride,
)

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _require_non_empty(value: str) -> str:
    """Reject a blank string field, failing fast at validation."""
    if not value.strip():
        raise ValueError("field must be a non-empty string")
    return value


class CalendarProvenance(BaseModel):
    """Immutable source attribution for a governed calendar fact (governance §8).

    Attributes:
        circular_id: The issuing exchange circular identifier (e.g. ``NSE/CMTR/71775``).
        circular_date: The circular's publication date.
        segment: The exchange segment the circular governs (e.g. ``Capital Market``).
        fact: The human-reviewable statement of what the circular substantiates; the
            ISO dates it names are the dates it attests.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    circular_id: str
    circular_date: date
    segment: str
    fact: str

    _non_empty = field_validator("circular_id", "segment", "fact")(_require_non_empty)


class IntervalSpec(BaseModel):
    """One raw ``[start, end)`` live-market interval as stored in the dataset.

    Interval validity (``start < end``) is enforced when the spec is projected onto the
    canonical :class:`~app.market_engine.session.TradingInterval` domain type, so the
    dataset never carries a second, divergent interval rule.

    Attributes:
        start: The exchange-local interval start (``HH:MM``).
        end: The exchange-local interval end (``HH:MM``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    start: time
    end: time


class SessionOverrideSpec(BaseModel):
    """A per-date exceptional-OPEN session's ordered live-market intervals.

    Attributes:
        trading_date: The exchange-local date the intervals apply to.
        intervals: The one-or-more live-market intervals (validated for ordering,
            non-overlap, and strict gaps by the domain override type).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    trading_date: date
    intervals: tuple[IntervalSpec, ...]


class TradingCalendarDataset(BaseModel):
    """A validated, version-controlled, broker-neutral trading-calendar dataset.

    Construction validates the whole dataset fail-fast: coverage ordering, in-coverage
    membership of every exception date, unique dates and overrides, override dates that
    are genuine OPEN sessions, OPEN/CLOSED disjointness, interval validity, and source
    attribution for every governed date (ADR-011 addendum M13/MI4). The builder methods
    project the validated primitives onto the Market-Engine domain types on demand.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dataset_id: str
    version: str
    segment: str
    coverage_start: date
    coverage_end: date
    closed_dates: tuple[date, ...]
    open_sessions: tuple[date, ...]
    session_overrides: tuple[SessionOverrideSpec, ...]
    provenance: tuple[CalendarProvenance, ...]

    _non_empty = field_validator("dataset_id", "version", "segment")(_require_non_empty)

    def calendar_coverage(self) -> CalendarCoverage:
        """Return the inclusive authoritative coverage window (fails fast if inverted)."""
        return CalendarCoverage(start_date=self.coverage_start, end_date=self.coverage_end)

    def trading_calendar(self) -> TradingCalendar:
        """Return the domain calendar built from closed dates and OPEN sessions.

        The returned :class:`~app.market_engine.session.TradingCalendar` is
        provenance-free by construction: attribution stays on this dataset only.
        """
        return TradingCalendar(closed_dates=self.closed_dates, open_sessions=self.open_sessions)

    def session_overrides_domain(self) -> tuple[TradingSessionOverride, ...]:
        """Return the per-date domain overrides (interval rules enforced by the type)."""
        return tuple(
            TradingSessionOverride(
                trading_date=spec.trading_date,
                live_intervals=tuple(
                    TradingInterval(start=iv.start, end=iv.end) for iv in spec.intervals
                ),
            )
            for spec in self.session_overrides
        )

    def effective_schedule(self, default: SessionSchedule) -> EffectiveSchedule:
        """Return the effective schedule layering this dataset's overrides on ``default``.

        Args:
            default: The canonical schedule used for any date without an override.

        Returns:
            An :class:`~app.market_engine.session.EffectiveSchedule` whose overrides are
            this dataset's per-date live intervals.
        """
        return EffectiveSchedule(default=default, overrides=self.session_overrides_domain())

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Run every dataset-level rule fail-fast, reusing the domain constructors."""
        coverage = self.calendar_coverage()
        self._validate_unique()
        self._validate_within_coverage(coverage)
        self._validate_overrides_reference_open()
        self.trading_calendar()
        self.session_overrides_domain()
        self._validate_provenance()
        return self

    def _validate_unique(self) -> None:
        """Reject duplicate closed dates, OPEN sessions, or override dates (MI4)."""
        for label, days in (("closed", self.closed_dates), ("open", self.open_sessions)):
            if len(set(days)) != len(days):
                raise ValueError(f"duplicate {label} dates are not permitted")
        override_dates = [spec.trading_date for spec in self.session_overrides]
        if len(set(override_dates)) != len(override_dates):
            raise ValueError("duplicate session override dates are not permitted")

    def _validate_within_coverage(self, coverage: CalendarCoverage) -> None:
        """Reject any closed, OPEN, or override date outside the coverage window (M6)."""
        for label, days in (("closed", self.closed_dates), ("open", self.open_sessions)):
            for day in days:
                if not coverage.contains(day):
                    raise ValueError(f"{label} date {day.isoformat()} lies outside coverage")
        for spec in self.session_overrides:
            if not coverage.contains(spec.trading_date):
                iso = spec.trading_date.isoformat()
                raise ValueError(f"override date {iso} lies outside coverage")

    def _validate_overrides_reference_open(self) -> None:
        """Reject an override whose date is not a declared OPEN session (M13)."""
        open_set = set(self.open_sessions)
        for spec in self.session_overrides:
            if spec.trading_date not in open_set:
                iso = spec.trading_date.isoformat()
                raise ValueError(f"session override {iso} is not a declared open session")

    def _validate_provenance(self) -> None:
        """Require source attribution for every governed calendar date (governance §8)."""
        if not self.provenance:
            raise ValueError("dataset requires at least one provenance entry")
        attested: set[date] = set()
        for entry in self.provenance:
            for match in _ISO_DATE.findall(entry.fact):
                attested.add(date.fromisoformat(match))
        required = set(self.closed_dates) | set(self.open_sessions)
        required |= {spec.trading_date for spec in self.session_overrides}
        missing = sorted(required - attested)
        if missing:
            joined = ", ".join(day.isoformat() for day in missing)
            raise ValueError(f"missing provenance attestation for dates: {joined}")
