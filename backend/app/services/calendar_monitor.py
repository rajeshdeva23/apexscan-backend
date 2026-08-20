"""Broker-neutral secondary calendar monitor: comparison, state, service, driver (ADR-011).

This is an OBSERVATION-ONLY monitor. It compares a secondary provider observation of the
exchange calendar against the authoritative :class:`TradingCalendarDataset` and records a
bounded operational state. It NEVER mutates the dataset, the trading calendar, coverage,
session overrides, or any live/historical classification, and it never resolves a
discrepancy automatically: a difference is a *possible calendar discrepancy requiring
authoritative NSE review*, never a calendar update.

The comparison is a pure function returning result objects; it only reads the dataset. The
:class:`CalendarObservationSource` / :class:`CalendarObservationParser` seams are structural
protocols (satisfied by the Dhan source/parser), so this module and the driver it defines
stay broker-neutral capabilities that the runtime can own without importing a provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from app.adapters.base.errors import ProviderBoundaryError
from app.adapters.dhan.calendar_monitor_parser import (
    CalendarObservationParseStatus,
    DhanCalendarObservation,
    ObservedCalendarDate,
    ObservedDateStatus,
)
from app.market_engine.calendar_data import TradingCalendarDataset
from app.market_engine.clock import Clock
from app.market_engine.historical.calendar_window import CalendarCoverage
from app.market_engine.session import TradingCalendar

logger = logging.getLogger(__name__)

_OBSERVATION_SOURCE = "dhan_market_holiday"
_MATCH_SIGNATURE = "match"


@runtime_checkable
class CalendarObservationSource(Protocol):
    """The narrow retrieval capability the monitor invokes (fetch raw page HTML)."""

    async def fetch(self) -> str:
        """Return the secondary calendar page's raw HTML."""


@runtime_checkable
class CalendarObservationParser(Protocol):
    """The narrow parse capability the monitor invokes (HTML to observation)."""

    def parse(self, html: str, *, source: str) -> DhanCalendarObservation:
        """Parse page HTML into a broker-neutral calendar observation."""


@runtime_checkable
class CalendarMonitorRun(Protocol):
    """The neutral capability the driver invokes once per scheduled run."""

    async def check(self, *, reference: datetime) -> CalendarMonitorState:
        """Fetch, parse, compare against the authoritative dataset, and update state."""


class CalendarComparisonStatus(StrEnum):
    """The outcome of comparing one secondary observation against the authority."""

    MATCH = "match"
    DHAN_NEW_CLOSED_DATE = "dhan_new_closed_date"
    DHAN_NEW_OPEN_DATE = "dhan_new_open_date"
    DHAN_DATE_STATUS_CONFLICT = "dhan_date_status_conflict"
    DHAN_SESSION_TIMING_CHANGE = "dhan_session_timing_change"
    DHAN_PARSE_FAILURE = "dhan_parse_failure"
    DHAN_FETCH_FAILURE = "dhan_fetch_failure"
    AUTHORITATIVE_COVERAGE_MISSING = "authoritative_coverage_missing"


class CalendarMonitorParseStatus(StrEnum):
    """The bounded parse status recorded in the monitor's operational state."""

    OK = "ok"
    PARSE_FAILURE = "parse_failure"
    NOT_ATTEMPTED = "not_attempted"


# Severity ordering for the overall status: the first present member wins (documented so
# a mixed diff always surfaces the most reviewable discrepancy first).
_SEVERITY_ORDER = (
    CalendarComparisonStatus.DHAN_DATE_STATUS_CONFLICT,
    CalendarComparisonStatus.DHAN_NEW_OPEN_DATE,
    CalendarComparisonStatus.DHAN_NEW_CLOSED_DATE,
    CalendarComparisonStatus.DHAN_SESSION_TIMING_CHANGE,
    CalendarComparisonStatus.AUTHORITATIVE_COVERAGE_MISSING,
)

_IntervalPairs = tuple[tuple[time, time], ...]


@dataclass(frozen=True, slots=True)
class _AuthorityView:
    """The read-only authority facts a comparison consults (never mutated)."""

    coverage: CalendarCoverage
    calendar: TradingCalendar
    open_set: set[date]
    closed_set: set[date]
    overrides: dict[date, _IntervalPairs]


@dataclass(frozen=True, slots=True)
class _DateFacts:
    """The per-date authority facts derived for one observed date."""

    day: date
    open_explicit: bool
    closed_explicit: bool
    trading: bool


@dataclass(frozen=True, slots=True)
class DateComparison:
    """One observed date's comparison result against the authority."""

    observed_date: date
    status: CalendarComparisonStatus


@dataclass(frozen=True, slots=True)
class CalendarComparison:
    """The whole comparison outcome for one observation (pure result object)."""

    overall_status: CalendarComparisonStatus
    date_results: tuple[DateComparison, ...]
    difference_count: int
    signature: str


@dataclass(frozen=True, slots=True)
class CalendarMonitorState:
    """Bounded operational state for the monitor (no HTML, no unbounded history)."""

    last_attempt_at: datetime | None
    last_success_at: datetime | None
    source: str | None
    source_year: int | None
    status: CalendarComparisonStatus | None
    difference_count: int
    parse_status: CalendarMonitorParseStatus
    signature: str | None


def _override_pairs(dataset: TradingCalendarDataset) -> dict[date, _IntervalPairs]:
    """Build a date to ordered ``(start, end)`` interval map from the dataset overrides."""
    return {
        spec.trading_date: tuple((interval.start, interval.end) for interval in spec.intervals)
        for spec in dataset.session_overrides
    }


def _overall_status(results: tuple[DateComparison, ...]) -> CalendarComparisonStatus:
    """Pick the highest-severity non-MATCH status, or MATCH when all agree."""
    present = {result.status for result in results}
    for status in _SEVERITY_ORDER:
        if status in present:
            return status
    return CalendarComparisonStatus.MATCH


def _signature(overall: CalendarComparisonStatus, results: tuple[DateComparison, ...]) -> str:
    """Return a deterministic, bounded signature of the discrepancy for re-alert dedup.

    An all-MATCH comparison yields the fixed ``"match"`` signature. Otherwise the signature
    is the SHA-256 hex of a canonical string of the overall status and the sorted
    ``(iso date, status)`` non-MATCH results, so identical discrepancies (and failures)
    map to identical signatures and a changed discrepancy maps to a different one.
    """
    if overall is CalendarComparisonStatus.MATCH:
        return _MATCH_SIGNATURE
    non_match = sorted(
        (result.observed_date.isoformat(), result.status.value)
        for result in results
        if result.status is not CalendarComparisonStatus.MATCH
    )
    canonical = overall.value + "|" + "|".join(f"{day}:{status}" for day, status in non_match)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _failure_comparison(status: CalendarComparisonStatus) -> CalendarComparison:
    """Build a comparison for a top-level failure (no per-date results)."""
    return CalendarComparison(
        overall_status=status,
        date_results=(),
        difference_count=0,
        signature=_signature(status, ()),
    )


def compare(
    observation: DhanCalendarObservation, dataset: TradingCalendarDataset | None
) -> CalendarComparison:
    """Compare one observation against the authority (pure; never mutates the dataset).

    Args:
        observation: The parsed secondary observation.
        dataset: The authoritative dataset, or ``None`` when unresolved.

    Returns:
        The whole comparison outcome. Top-level short-circuits: a parse failure yields
        ``DHAN_PARSE_FAILURE`` and a ``None`` dataset yields ``AUTHORITATIVE_COVERAGE_MISSING``,
        both with empty results.
    """
    if observation.parse_status is CalendarObservationParseStatus.PARSE_FAILURE:
        return _failure_comparison(CalendarComparisonStatus.DHAN_PARSE_FAILURE)
    if dataset is None:
        return _failure_comparison(CalendarComparisonStatus.AUTHORITATIVE_COVERAGE_MISSING)
    authority = _AuthorityView(
        coverage=dataset.calendar_coverage(),
        calendar=dataset.trading_calendar(),
        open_set=set(dataset.open_sessions),
        closed_set=set(dataset.closed_dates),
        overrides=_override_pairs(dataset),
    )
    results = tuple(_compare_date(entry, authority) for entry in observation.dates)
    difference_count = sum(
        1 for result in results if result.status is not CalendarComparisonStatus.MATCH
    )
    overall = _overall_status(results)
    return CalendarComparison(
        overall_status=overall,
        date_results=results,
        difference_count=difference_count,
        signature=_signature(overall, results),
    )


def _compare_date(entry: ObservedCalendarDate, authority: _AuthorityView) -> DateComparison:
    """Classify one observed date against the authority (reads only; no mutation)."""
    day = entry.observed_date
    if not authority.coverage.contains(day):
        return DateComparison(day, CalendarComparisonStatus.AUTHORITATIVE_COVERAGE_MISSING)
    facts = _DateFacts(
        day=day,
        open_explicit=day in authority.open_set,
        closed_explicit=day in authority.closed_set,
        trading=authority.calendar.is_trading_day(day),
    )
    if entry.status is ObservedDateStatus.CLOSED:
        return _compare_closed(facts)
    return _compare_open(entry, facts, authority.overrides)


def _compare_closed(facts: _DateFacts) -> DateComparison:
    """Classify a date the observation reports CLOSED."""
    if facts.open_explicit:
        return DateComparison(facts.day, CalendarComparisonStatus.DHAN_DATE_STATUS_CONFLICT)
    if not facts.trading:
        return DateComparison(facts.day, CalendarComparisonStatus.MATCH)
    return DateComparison(facts.day, CalendarComparisonStatus.DHAN_NEW_CLOSED_DATE)


def _compare_open(
    entry: ObservedCalendarDate, facts: _DateFacts, overrides: dict[date, _IntervalPairs]
) -> DateComparison:
    """Classify a date the observation reports OPEN."""
    if facts.closed_explicit:
        return DateComparison(facts.day, CalendarComparisonStatus.DHAN_DATE_STATUS_CONFLICT)
    if facts.open_explicit:
        return _compare_open_timing(entry, facts.day, overrides)
    if not facts.trading:
        return DateComparison(facts.day, CalendarComparisonStatus.DHAN_NEW_OPEN_DATE)
    return DateComparison(facts.day, CalendarComparisonStatus.MATCH)


def _compare_open_timing(
    entry: ObservedCalendarDate, day: date, overrides: dict[date, _IntervalPairs]
) -> DateComparison:
    """Compare observed OPEN timing against an authority override (secondary evidence only)."""
    if not entry.intervals:
        return DateComparison(day, CalendarComparisonStatus.MATCH)
    observed = tuple((interval.start, interval.end) for interval in entry.intervals)
    authority = overrides.get(day)
    if authority is not None and authority == observed:
        return DateComparison(day, CalendarComparisonStatus.MATCH)
    return DateComparison(day, CalendarComparisonStatus.DHAN_SESSION_TIMING_CHANGE)


class CalendarMonitorService:
    """Fetch, parse, and compare the secondary calendar; hold bounded state (ADR-011)."""

    def __init__(
        self,
        *,
        source: CalendarObservationSource,
        parser: CalendarObservationParser,
        dataset: TradingCalendarDataset | None,
        clock: Clock,
    ) -> None:
        """Wire the monitor to its source, parser, the resolved dataset, and a clock.

        Args:
            source: The retrieval capability (the same injected transport as the runtime).
            parser: The HTML-to-observation parser.
            dataset: The SAME resolved dataset the runtime uses; ``None`` fails closed to
                ``AUTHORITATIVE_COVERAGE_MISSING`` (never a provider fallback).
            clock: The injected UTC clock (the scheduled run supplies the reference).
        """
        self._source = source
        self._parser = parser
        self._dataset = dataset
        self._clock = clock
        self._state = CalendarMonitorState(
            last_attempt_at=None,
            last_success_at=None,
            source=None,
            source_year=None,
            status=None,
            difference_count=0,
            parse_status=CalendarMonitorParseStatus.NOT_ATTEMPTED,
            signature=None,
        )

    def state(self) -> CalendarMonitorState:
        """Return the current bounded operational state."""
        return self._state

    async def check(self, *, reference: datetime) -> CalendarMonitorState:
        """Run one observation cycle and return the updated state (never raises upward).

        Provider fetch failures are caught and become ``DHAN_FETCH_FAILURE`` so the driver
        survives. Exactly one structured log line is emitted per cycle (INFO when the
        discrepancy signature changed, DEBUG when unchanged). The dataset is never mutated.
        """
        previous_signature = self._state.signature
        self._state = await self._evaluate(reference)
        self._log(previous_signature)
        return self._state

    async def _evaluate(self, reference: datetime) -> CalendarMonitorState:
        """Fetch, parse, and compare; map every outcome to a bounded state."""
        try:
            html = await self._source.fetch()
        except ProviderBoundaryError:
            return self._fetch_failure_state(reference)
        observation = self._parser.parse(html, source=_OBSERVATION_SOURCE)
        comparison = compare(observation, self._dataset)
        return self._success_state(reference, observation, comparison)

    def _fetch_failure_state(self, reference: datetime) -> CalendarMonitorState:
        """Build the state for a fetch failure, retaining prior success attribution."""
        prior = self._state
        status = CalendarComparisonStatus.DHAN_FETCH_FAILURE
        return CalendarMonitorState(
            last_attempt_at=reference,
            last_success_at=prior.last_success_at,
            source=prior.source,
            source_year=prior.source_year,
            status=status,
            difference_count=0,
            parse_status=CalendarMonitorParseStatus.NOT_ATTEMPTED,
            signature=_signature(status, ()),
        )

    def _success_state(
        self,
        reference: datetime,
        observation: DhanCalendarObservation,
        comparison: CalendarComparison,
    ) -> CalendarMonitorState:
        """Build the state for a completed fetch (OK or parse-failure comparison)."""
        parse_status = (
            CalendarMonitorParseStatus.OK
            if observation.parse_status is CalendarObservationParseStatus.OK
            else CalendarMonitorParseStatus.PARSE_FAILURE
        )
        return CalendarMonitorState(
            last_attempt_at=reference,
            last_success_at=reference,
            source=observation.source,
            source_year=observation.source_year,
            status=comparison.overall_status,
            difference_count=comparison.difference_count,
            parse_status=parse_status,
            signature=comparison.signature,
        )

    def _log(self, previous_signature: str | None) -> None:
        """Emit one structured log line, escalating to INFO when the signature changed."""
        state = self._state
        level = logging.INFO if state.signature != previous_signature else logging.DEBUG
        logger.log(
            level,
            "calendar monitor check: status=%s differences=%d source=%s parse=%s signature=%s",
            state.status.value if state.status is not None else "none",
            state.difference_count,
            state.source,
            state.parse_status.value,
            state.signature,
        )


class CalendarMonitorDriver:
    """Fire the monitor at most once per exchange-local calendar day at/after a run time."""

    def __init__(
        self,
        *,
        monitor: CalendarMonitorRun,
        clock: Clock,
        exchange_timezone: str,
        run_time: time,
        poll_seconds: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Wire the driver to the monitor, clock, exchange timezone, and daily run time.

        Args:
            monitor: The neutral monitor capability to invoke.
            clock: The injected UTC clock (production uses the system clock).
            exchange_timezone: The IANA timezone the run time is expressed in.
            run_time: The exchange-local time at/after which the daily run fires.
            poll_seconds: The infrastructure wake interval (not the run cadence).
            sleep: The wait seam; injectable for deterministic tests.
        """
        self._monitor = monitor
        self._clock = clock
        self._timezone = ZoneInfo(exchange_timezone)
        self._run_time = run_time
        self._poll_seconds = poll_seconds
        self._sleep = sleep
        self._last_run_date: date | None = None

    async def run(self) -> None:
        """Loop: fire the daily run when due, then wait, until cancelled on shutdown."""
        while True:
            await self._maybe_check()
            await self._sleep(self._poll_seconds)

    async def _maybe_check(self) -> None:
        """Fire ``check`` once when the exchange-local day rolls to at/after the run time."""
        now = self._clock.now()
        local = now.astimezone(self._timezone)
        local_date = local.date()
        if local_date != self._last_run_date and local.time() >= self._run_time:
            await self._monitor.check(reference=now)
            self._last_run_date = local_date
