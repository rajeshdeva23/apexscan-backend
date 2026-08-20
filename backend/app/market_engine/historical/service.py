"""DIRECT historical warmup orchestration and per-instrument status (P4.5B).

Given the known instruments, the effective requirements, a calendar window, and a
source's direct-timeframe capabilities, the service plans deterministic fetches for
directly-supported timeframes, satisfies them through the coordinator (cache +
dedup + bounded concurrency), trims to each requirement's lookback, assembles one
immutable :class:`HistoricalContext` per instrument, and installs it atomically —
minting no MarketContext version (that surfaces on the next accepted datum, P4.5A).

Non-directly-supported timeframes (e.g. 7m) are recorded as reconstruction-pending
for P4.5C; no source request is made for them and they are never a provider failure.
Warmup never depends on a current-day bar: windows are resolved over *previous*
completed trading sessions (CURRENT_DAY_RECONCILIATION_GUARANTEE remains NOT PROVEN).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.market_engine.candle_engine import CandleEngine
from app.market_engine.context import IncompleteCandle
from app.market_engine.historical.calendar_window import (
    HistoricalCalendarWindow,
    MissingSessionTimingError,
)
from app.market_engine.historical.context import (
    HistoricalContext,
    HistoricalSeries,
    PreviousSessionFacts,
)
from app.market_engine.historical.coordinator import HistoricalCoordinator
from app.market_engine.historical.reconciliation import (
    ReconciliationOutcome,
    ReconciliationResult,
    ReconciliationSummary,
    identity_of,
    identity_of_incomplete,
)
from app.market_engine.historical.requirements import (
    HistoricalRequirement,
    timeframe_ordering_key,
)
from app.market_engine.historical.resampling import reconstruct_series, select_base
from app.market_engine.historical.session_candles import canonical_session_series
from app.market_engine.historical.source import (
    HistoricalDataQualityError,
    HistoricalFetchPlan,
    HistoricalRequestKey,
    HistoricalSourceError,
    interval_for_timeframe,
)
from app.market_engine.session import (
    EffectiveSchedule,
    SessionSchedule,
    TradingCalendar,
    TradingInterval,
    TradingSessionOverride,
)
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, Instrument

_DEFAULT_SESSION_MARGIN = 1
_DURATION_EPOCH = date(2000, 1, 1)


class WarmupState(StrEnum):
    """The terminal (or in-progress) DIRECT-requirement warmup state for an instrument.

    ``pending_reconstruction`` timeframes are tracked separately and never change
    this state — a reconstruction-pending 7m is not a DIRECT failure (ADR-006 §7).
    """

    NOT_STARTED = "not_started"
    WARMING = "warming"
    SATISFIED = "satisfied"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class InstrumentWarmupStatus:
    """Immutable per-instrument warmup outcome.

    Attributes:
        instrument: The instrument this status describes.
        state: The DIRECT-requirement warmup state.
        satisfied: Directly-supported timeframes that were fully satisfied.
        unresolved: Directly-supported timeframes that failed or under-fetched.
        pending_reconstruction: Non-directly-supported timeframes deferred to P4.5C.
    """

    instrument: Instrument
    state: WarmupState
    satisfied: tuple[Timeframe, ...]
    unresolved: tuple[Timeframe, ...]
    pending_reconstruction: tuple[Timeframe, ...]


def _instrument_sort_key(instrument: Instrument) -> tuple[str, str]:
    """Return a deterministic ordering key for an instrument."""
    return (instrument.exchange, instrument.symbol)


def _sorted_instruments(instruments: Iterable[Instrument]) -> tuple[Instrument, ...]:
    """Return the instruments in a deterministic order."""
    return tuple(sorted(instruments, key=_instrument_sort_key))


def _sorted_requirements(
    requirements: Iterable[HistoricalRequirement],
) -> tuple[HistoricalRequirement, ...]:
    """Return the requirements in deterministic timeframe order."""
    return tuple(sorted(requirements, key=lambda req: timeframe_ordering_key(req.timeframe)))


class HistoricalRangePlanner:
    """Resolves deterministic, timezone-aware fetch windows over completed sessions."""

    def __init__(
        self,
        *,
        schedule: SessionSchedule,
        exchange_timezone: str,
        calendar_window: HistoricalCalendarWindow,
        session_margin: int = _DEFAULT_SESSION_MARGIN,
        overrides: Iterable[TradingSessionOverride] = (),
    ) -> None:
        """Wire the planner to a session schedule, timezone, and calendar window.

        Args:
            schedule: The exchange session boundaries (regular open/close used).
            exchange_timezone: The IANA exchange timezone (e.g. "Asia/Kolkata").
            calendar_window: Previous-trading-day resolver over authoritative coverage.
            session_margin: Extra completed sessions to over-fetch for intraday safety.
            overrides: Per-date session-hour overrides for exceptional OPEN sessions;
                empty preserves the ordinary single-schedule behaviour exactly.
        """
        self._schedule = schedule
        self._exchange_timezone = exchange_timezone
        self._timezone = ZoneInfo(exchange_timezone)
        self._window = calendar_window
        self._margin = session_margin
        self._effective = EffectiveSchedule(default=schedule, overrides=overrides)

    @property
    def schedule(self) -> SessionSchedule:
        """Return the exchange session schedule."""
        return self._schedule

    @property
    def effective_schedule(self) -> EffectiveSchedule:
        """Return the default-plus-override effective session-bounds resolver."""
        return self._effective

    @property
    def exchange_timezone(self) -> str:
        """Return the IANA exchange timezone name."""
        return self._exchange_timezone

    @property
    def calendar(self) -> TradingCalendar:
        """Return the trading calendar backing the window."""
        return self._window.calendar

    def anchor_date(self, reference: datetime) -> date:
        """Return the exchange-local date the reference instant falls on."""
        return reference.astimezone(self._timezone).date()

    def local_date(self, instant: datetime) -> date:
        """Return the exchange-local trading date an instant belongs to."""
        return instant.astimezone(self._timezone).date()

    def session_bounds(self, trading_date: date) -> tuple[datetime, datetime]:
        """Return the UTC ``(open, close)`` bounds of one trading session."""
        return (
            self._localize(trading_date, self._schedule.regular_open),
            self._localize(trading_date, self._schedule.regular_close),
        )

    def newest_completed_session(self, reference: datetime) -> date:
        """Return the most recent completed trading session before the reference date."""
        return self._window.previous_trading_day(self.anchor_date(reference))

    def resolve(
        self, requirement: HistoricalRequirement, reference: datetime
    ) -> tuple[datetime, datetime]:
        """Resolve the UTC window that safely covers the requirement's lookback.

        Ordinary calendars (no in-window OPEN override) resolve exactly as before. When
        the resolved window includes an exceptional OPEN session and the requirement is
        intraday, resolution consults per-date effective bounds and fails closed if any
        special session lacks session-hours metadata (ADR-011 addendum M15/M16).

        Args:
            requirement: The requirement whose window is being resolved.
            reference: The deterministic reference instant (UTC, tz-aware).

        Returns:
            A ``(start, end)`` pair of timezone-aware UTC instants over previous
            completed sessions (never the current, possibly-incomplete session).

        Raises:
            MissingSessionTimingError: If an intraday window includes an OPEN session
                that lacks per-date session-hours metadata.
        """
        anchor = self.anchor_date(reference)
        dates = self._window.previous_trading_days(anchor, self._sessions_needed(requirement))
        special = tuple(day for day in dates if day in self.calendar.open_sessions)
        if requirement.timeframe.is_session or not special:
            return self._localize_default(dates)
        for day in special:
            self._guard_special(day)
        return self._resolve_intraday_over_special(requirement, anchor)

    def _localize_default(self, dates: tuple[date, ...]) -> tuple[datetime, datetime]:
        """Localize a resolved date set with the default schedule bounds (unchanged path)."""
        start = self._localize(dates[0], self._schedule.regular_open)
        end = self._localize(dates[-1], self._schedule.regular_close)
        return start, end

    def _resolve_intraday_over_special(
        self, requirement: HistoricalRequirement, anchor: date
    ) -> tuple[datetime, datetime]:
        """Recompute an intraday window using each date's effective intraday capacity."""
        interval_seconds = interval_for_timeframe(requirement.timeframe).total_seconds()
        dates: list[date] = []
        cumulative = 0
        cursor = anchor
        while cumulative < requirement.lookback:
            cursor = self._window.previous_trading_day(cursor)
            self._guard_special(cursor)
            dates.append(cursor)
            cumulative += self._capacity_for(cursor, interval_seconds)
        for _ in range(self._margin):
            cursor = self._window.previous_trading_day(cursor)
            self._guard_special(cursor)
            dates.append(cursor)
        dates.reverse()
        return (
            self._localize(dates[0], self._effective.envelope_for(dates[0]).start),
            self._localize(dates[-1], self._effective.envelope_for(dates[-1]).end),
        )

    def _guard_special(self, day: date) -> None:
        """Fail closed if a special OPEN date lacks per-date session-hours metadata."""
        if day in self.calendar.open_sessions and not self._effective.has_override(day):
            raise MissingSessionTimingError(
                f"OPEN session {day.isoformat()} lacks intraday session-hours metadata"
            )

    def _sessions_needed(self, requirement: HistoricalRequirement) -> int:
        if requirement.timeframe.is_session:
            return requirement.lookback
        per_session = self._candles_per_session(requirement.timeframe)
        needed = -(-requirement.lookback // per_session)  # ceil division
        return needed + self._margin

    def _candles_per_session(self, timeframe: Timeframe) -> int:
        interval_seconds = interval_for_timeframe(timeframe).total_seconds()
        count = int(self._session_seconds() // interval_seconds)
        return max(count, 1)

    def _capacity_for(self, day: date, interval_seconds: float) -> int:
        """Return a date's intraday capacity: summed per-interval capacity (MI7/MI12).

        Each live interval contributes ``floor(interval_seconds / delta)`` buckets
        (at least one), and the closed gap between intervals contributes zero — the
        capacity is never computed from the whole-day envelope.
        """
        total = 0
        for interval in self._effective.intervals_for(day):
            count = int(self._interval_seconds(interval) // interval_seconds)
            total += max(count, 1)
        return total

    def _session_seconds(self) -> float:
        return self._interval_seconds(self._schedule.bounds)

    @staticmethod
    def _interval_seconds(interval: TradingInterval) -> float:
        opened = datetime.combine(_DURATION_EPOCH, interval.start)
        closed = datetime.combine(_DURATION_EPOCH, interval.end)
        return (closed - opened).total_seconds()

    def _localize(self, day: date, moment: time) -> datetime:
        return datetime.combine(day, moment).replace(tzinfo=self._timezone).astimezone(UTC)


def plan_direct_fetches(
    *,
    instruments: Iterable[Instrument],
    effective_requirements: Iterable[HistoricalRequirement],
    direct_timeframes: frozenset[Timeframe],
    planner: HistoricalRangePlanner,
    reference: datetime,
) -> tuple[HistoricalFetchPlan, ...]:
    """Return the deterministic direct-timeframe fetch plans (pure, no I/O).

    One plan per (instrument, directly-supported effective requirement). Non-direct
    timeframes yield no plan. Plans carry no consumer key, strategy, or provider id.

    Args:
        instruments: The instruments to plan for.
        effective_requirements: The deduplicated union of requirements.
        direct_timeframes: Timeframes the source supports directly.
        planner: The range planner resolving each window.
        reference: The deterministic reference instant.

    Returns:
        A deterministically ordered tuple of fetch plans.
    """
    requirements = _sorted_requirements(effective_requirements)
    plans: list[HistoricalFetchPlan] = []
    for instrument in _sorted_instruments(instruments):
        for requirement in requirements:
            if requirement.timeframe not in direct_timeframes:
                continue
            start, end = planner.resolve(requirement, reference)
            plans.append(
                HistoricalFetchPlan(
                    instrument=instrument,
                    requirement=requirement,
                    start=start,
                    end=end,
                    interval=interval_for_timeframe(requirement.timeframe),
                )
            )
    return tuple(plans)


@dataclass(frozen=True, slots=True)
class _Descriptor:
    """How one instrument's requirement will be satisfied (internal to warmup)."""

    requirement: HistoricalRequirement
    kind: str  # "direct" | "reconstruct" | "pending"
    plan: HistoricalFetchPlan | None
    base: Timeframe | None


class HistoricalWarmupService:
    """Orchestrates direct and reconstructed historical warmup and per-instrument status."""

    def __init__(
        self,
        *,
        registry: InstrumentStateRegistry,
        coordinator: HistoricalCoordinator,
        planner: HistoricalRangePlanner,
        candles: CandleEngine | None = None,
        supports_current_day: bool = False,
    ) -> None:
        """Wire the service to the state registry, coordinator, planner, and engine.

        Args:
            registry: The per-instrument state registry.
            coordinator: The cached, deduplicated historical fetch coordinator.
            planner: The deterministic range planner.
            candles: The live candle engine whose incomplete intervals are repaired
                (reconciliation is a no-op when absent).
            supports_current_day: Whether the provider has been explicitly verified
                to deliver timely current-day completed history. Defaults to False
                (CURRENT_DAY_RECONCILIATION_GUARANTEE remains NOT PROVEN).
        """
        self._registry = registry
        self._coordinator = coordinator
        self._planner = planner
        self._candles = candles
        self._supports_current_day = supports_current_day
        self._status: dict[Instrument, InstrumentWarmupStatus] = {}

    def status_for(self, instrument: Instrument) -> InstrumentWarmupStatus:
        """Return the current warmup status for an instrument (NOT_STARTED if none)."""
        existing = self._status.get(instrument)
        if existing is not None:
            return existing
        return InstrumentWarmupStatus(
            instrument=instrument,
            state=WarmupState.NOT_STARTED,
            satisfied=(),
            unresolved=(),
            pending_reconstruction=(),
        )

    def snapshot(self) -> dict[Instrument, InstrumentWarmupStatus]:
        """Return a copy of the current per-instrument warmup statuses."""
        return dict(self._status)

    async def warmup(
        self,
        instruments: Iterable[Instrument],
        effective_requirements: Iterable[HistoricalRequirement],
        *,
        reference: datetime,
    ) -> dict[Instrument, InstrumentWarmupStatus]:
        """Warm direct historical context for each instrument and return statuses.

        Assembly is deterministic and independent of source completion order.

        Args:
            instruments: The instruments to warm.
            effective_requirements: The deduplicated union of requirements.
            reference: The deterministic reference instant (UTC, tz-aware).

        Returns:
            A snapshot of per-instrument warmup statuses.
        """
        ordered_instruments = _sorted_instruments(instruments)
        requirements = _sorted_requirements(effective_requirements)
        direct = self._coordinator.direct_timeframes
        descriptors: dict[Instrument, tuple[_Descriptor, ...]] = {}
        plans: dict[HistoricalRequestKey, HistoricalFetchPlan] = {}
        for instrument in ordered_instruments:
            self._status[instrument] = _warming_status(instrument)
            instrument_descriptors = tuple(
                self._classify(instrument, requirement, direct, reference)
                for requirement in requirements
            )
            descriptors[instrument] = instrument_descriptors
            for descriptor in instrument_descriptors:
                if descriptor.plan is not None:
                    plans[descriptor.plan.key] = descriptor.plan
        fetched = await self._fetch_all(tuple(plans.values()))
        for instrument in ordered_instruments:
            self._status[instrument] = self._assemble(
                instrument, descriptors[instrument], fetched, reference
            )
        active = frozenset(plan.requirement.timeframe for plan in plans.values())
        self._coordinator.retain_timeframes(active)
        return self.snapshot()

    def _classify(
        self,
        instrument: Instrument,
        requirement: HistoricalRequirement,
        direct: frozenset[Timeframe],
        reference: datetime,
    ) -> _Descriptor:
        """Classify one requirement as direct, reconstructable, pending, or failed."""
        timeframe = requirement.timeframe
        if timeframe in direct:
            return self._planned(
                instrument, requirement, timeframe, kind="direct", base=None, reference=reference
            )
        base = select_base(timeframe, direct)
        if base is None:
            return _Descriptor(requirement=requirement, kind="pending", plan=None, base=None)
        return self._planned(
            instrument, requirement, base, kind="reconstruct", base=base, reference=reference
        )

    def _planned(
        self,
        instrument: Instrument,
        requirement: HistoricalRequirement,
        fetch_timeframe: Timeframe,
        *,
        kind: str,
        base: Timeframe | None,
        reference: datetime,
    ) -> _Descriptor:
        """Build a planned descriptor, failing closed on missing session timing (M11)."""
        try:
            plan = self._fetch_plan(instrument, requirement, fetch_timeframe, reference)
        except MissingSessionTimingError:
            return _Descriptor(requirement=requirement, kind="failed", plan=None, base=None)
        return _Descriptor(requirement=requirement, kind=kind, plan=plan, base=base)

    def _fetch_plan(
        self,
        instrument: Instrument,
        requirement: HistoricalRequirement,
        fetch_timeframe: Timeframe,
        reference: datetime,
    ) -> HistoricalFetchPlan:
        """Build a fetch plan covering the requirement's window at ``fetch_timeframe``."""
        start, end = self._planner.resolve(requirement, reference)
        return HistoricalFetchPlan(
            instrument=instrument,
            requirement=HistoricalRequirement(
                timeframe=fetch_timeframe, lookback=requirement.lookback
            ),
            start=start,
            end=end,
            interval=interval_for_timeframe(fetch_timeframe),
        )

    async def _fetch_all(
        self, plans: tuple[HistoricalFetchPlan, ...]
    ) -> dict[HistoricalRequestKey, tuple[Candle, ...] | None]:
        results = await asyncio.gather(*(self._safe_fetch(plan) for plan in plans))
        return dict(results)

    async def _safe_fetch(
        self, plan: HistoricalFetchPlan
    ) -> tuple[HistoricalRequestKey, tuple[Candle, ...] | None]:
        """Fetch one plan, isolating a source/quality failure to that plan."""
        try:
            candles = await self._coordinator.fetch(plan)
        except (HistoricalSourceError, HistoricalDataQualityError):
            return (plan.key, None)
        return (plan.key, candles)

    def _assemble(
        self,
        instrument: Instrument,
        descriptors: tuple[_Descriptor, ...],
        fetched: dict[HistoricalRequestKey, tuple[Candle, ...] | None],
        reference: datetime,
    ) -> InstrumentWarmupStatus:
        """Assemble and install one instrument's HistoricalContext, returning its status."""
        series: list[HistoricalSeries] = []
        satisfied: list[Timeframe] = []
        unresolved: list[Timeframe] = []
        pending: list[Timeframe] = []
        previous: PreviousSessionFacts | None = None
        for descriptor in descriptors:
            timeframe = descriptor.requirement.timeframe
            if descriptor.kind == "pending":
                pending.append(timeframe)
                continue
            if descriptor.kind == "failed":
                unresolved.append(timeframe)
                continue
            built = self._build_series(descriptor, fetched)
            if built is None:
                unresolved.append(timeframe)
                continue
            series.append(built)
            satisfied.append(timeframe)
            if timeframe.is_session:
                previous = self._previous_session(built, reference)
        context = HistoricalContext(
            instrument=instrument, previous_session=previous, series=tuple(series)
        )
        self._registry.install_historical(instrument, context)
        return _terminal_status(instrument, satisfied, unresolved, pending)

    def _build_series(
        self,
        descriptor: _Descriptor,
        fetched: dict[HistoricalRequestKey, tuple[Candle, ...] | None],
    ) -> HistoricalSeries | None:
        """Build the trimmed target series for a direct or reconstructable requirement."""
        candles = fetched.get(descriptor.plan.key) if descriptor.plan is not None else None
        if descriptor.kind == "direct":
            return self._direct_series(descriptor.requirement, candles)
        return self._reconstructed_series(descriptor, candles)

    def _direct_series(
        self, requirement: HistoricalRequirement, candles: tuple[Candle, ...] | None
    ) -> HistoricalSeries | None:
        if candles is None:
            return None
        prepared = self._prepare_candles(requirement.timeframe, candles)
        if len(prepared) < requirement.lookback:
            return None
        return HistoricalSeries(
            timeframe=requirement.timeframe, candles=prepared[-requirement.lookback :]
        )

    def _prepare_candles(
        self, timeframe: Timeframe, candles: tuple[Candle, ...]
    ) -> tuple[Candle, ...]:
        """Canonicalize session bars to session identity; return candles oldest-first."""
        if timeframe.is_session:
            candles = canonical_session_series(
                candles,
                effective=self._planner.effective_schedule,
                calendar=self._planner.calendar,
                exchange_timezone=self._planner.exchange_timezone,
            )
        return tuple(sorted(candles, key=lambda candle: candle.start_timestamp))

    def _reconstructed_series(
        self, descriptor: _Descriptor, base_candles: tuple[Candle, ...] | None
    ) -> HistoricalSeries | None:
        if not base_candles or descriptor.base is None:
            return None
        try:
            source = HistoricalSeries(timeframe=descriptor.base, candles=base_candles)
        except ValidationError:
            return None
        rebuilt = reconstruct_series(
            source=source,
            target=descriptor.requirement.timeframe,
            effective=self._planner.effective_schedule,
            calendar=self._planner.calendar,
            exchange_timezone=self._planner.exchange_timezone,
        )
        lookback = descriptor.requirement.lookback
        if rebuilt is None or len(rebuilt.candles) < lookback:
            return None
        return HistoricalSeries(
            timeframe=descriptor.requirement.timeframe, candles=rebuilt.candles[-lookback:]
        )

    def _previous_session(
        self, session_series: HistoricalSeries, reference: datetime
    ) -> PreviousSessionFacts:
        """Derive previous-session facts from the most recent session candle (no refetch)."""
        return PreviousSessionFacts(
            trading_date=self._planner.newest_completed_session(reference),
            candle=session_series.candles[-1],
        )

    async def reconcile_completed(
        self, instrument: Instrument, *, reference: datetime
    ) -> ReconciliationSummary:
        """Reconcile an instrument's completed incomplete intervals with authoritative history.

        Repairs only completed prior-session intervals (current-day is withheld
        unless the provider is verified). Reuses the P4.5B/P4.5C cache/coordinator;
        the active partial and MarketContext versioning are untouched.

        Args:
            instrument: The instrument to reconcile.
            reference: The deterministic reference instant (UTC, tz-aware).

        Returns:
            A :class:`ReconciliationSummary` of per-interval outcomes.
        """
        engine = self._candles
        if engine is None:
            return ReconciliationSummary(instrument=instrument, results=())
        anchor = self._planner.anchor_date(reference)
        results: list[ReconciliationResult] = []
        for candles in engine.candle_sets_for(instrument):
            results.extend(
                await self._reconcile_timeframe(
                    engine, instrument, candles.timeframe, candles.incomplete, anchor
                )
            )
        return ReconciliationSummary(instrument=instrument, results=tuple(results))

    async def _reconcile_timeframe(
        self,
        engine: CandleEngine,
        instrument: Instrument,
        timeframe: Timeframe,
        incomplete: tuple[IncompleteCandle, ...],
        anchor: date,
    ) -> list[ReconciliationResult]:
        """Repair every reconcilable incomplete interval for one timeframe."""
        withheld = [
            ReconciliationResult(
                identity_of_incomplete(item), ReconciliationOutcome.CURRENT_DAY_WITHHELD
            )
            for item in incomplete
            if self._repair_state(item, anchor) == "current_day"
        ]
        repairable = [item for item in incomplete if self._repair_state(item, anchor) == "repair"]
        if not repairable:
            return withheld
        series = await self._authoritative_series(instrument, timeframe, repairable)
        if series is None:
            return withheld + [
                ReconciliationResult(
                    identity_of_incomplete(item), ReconciliationOutcome.NO_AUTHORITATIVE_CANDLE
                )
                for item in repairable
            ]
        return withheld + self._apply_repairs(engine, timeframe, repairable, series)

    def _repair_state(self, incomplete: IncompleteCandle, anchor: date) -> str:
        """Classify an incomplete interval as ``repair``, ``current_day``, or ``skip``."""
        trading_date = self._planner.local_date(incomplete.start_timestamp)
        if not self._planner.calendar.is_trading_day(trading_date) or trading_date > anchor:
            return "skip"  # holiday/weekend or future — fail closed
        if trading_date == anchor and not self._supports_current_day:
            return "current_day"
        return "repair"

    @staticmethod
    def _apply_repairs(
        engine: CandleEngine,
        timeframe: Timeframe,
        repairable: list[IncompleteCandle],
        series: HistoricalSeries,
    ) -> list[ReconciliationResult]:
        """Reconcile each incomplete interval against an authoritative series by exact identity."""
        by_identity = {identity_of(candle, timeframe): candle for candle in series.candles}
        results: list[ReconciliationResult] = []
        for item in repairable:
            identity = identity_of_incomplete(item)
            authoritative = by_identity.get(identity)
            if authoritative is None:
                results.append(
                    ReconciliationResult(identity, ReconciliationOutcome.NO_AUTHORITATIVE_CANDLE)
                )
            else:
                results.append(engine.reconcile(authoritative, timeframe))
        return results

    async def _authoritative_series(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        repairable: list[IncompleteCandle],
    ) -> HistoricalSeries | None:
        """Fetch or reconstruct the authoritative series covering the repairable dates."""
        dates = sorted({self._planner.local_date(item.start_timestamp) for item in repairable})
        start, _ = self._planner.session_bounds(dates[0])
        _, end = self._planner.session_bounds(dates[-1])
        direct = self._coordinator.direct_timeframes
        if timeframe in direct:
            candles = await self._safe_candles(instrument, timeframe, start, end)
            if not candles:
                return None
            prepared = self._prepare_candles(timeframe, candles)
            return HistoricalSeries(timeframe=timeframe, candles=prepared) if prepared else None
        base = select_base(timeframe, direct)
        if base is None:
            return None
        base_candles = await self._safe_candles(instrument, base, start, end)
        if not base_candles:
            return None
        return reconstruct_series(
            source=HistoricalSeries(timeframe=base, candles=base_candles),
            target=timeframe,
            effective=self._planner.effective_schedule,
            calendar=self._planner.calendar,
            exchange_timezone=self._planner.exchange_timezone,
        )

    async def _safe_candles(
        self, instrument: Instrument, timeframe: Timeframe, start: datetime, end: datetime
    ) -> tuple[Candle, ...] | None:
        """Fetch a window through the coordinator, isolating source/quality failures."""
        plan = HistoricalFetchPlan(
            instrument=instrument,
            requirement=HistoricalRequirement(timeframe=timeframe, lookback=1),
            start=start,
            end=end,
            interval=interval_for_timeframe(timeframe),
        )
        try:
            return await self._coordinator.fetch(plan)
        except (HistoricalSourceError, HistoricalDataQualityError):
            return None


def _warming_status(instrument: Instrument) -> InstrumentWarmupStatus:
    """Return the transient WARMING status for an instrument."""
    return InstrumentWarmupStatus(
        instrument=instrument,
        state=WarmupState.WARMING,
        satisfied=(),
        unresolved=(),
        pending_reconstruction=(),
    )


def _terminal_status(
    instrument: Instrument,
    satisfied: list[Timeframe],
    unresolved: list[Timeframe],
    pending: list[Timeframe],
) -> InstrumentWarmupStatus:
    """Build the terminal warmup status from classified timeframes."""
    if unresolved and satisfied:
        state = WarmupState.PARTIAL
    elif unresolved:
        state = WarmupState.FAILED
    else:
        state = WarmupState.SATISFIED
    return InstrumentWarmupStatus(
        instrument=instrument,
        state=state,
        satisfied=tuple(satisfied),
        unresolved=tuple(unresolved),
        pending_reconstruction=tuple(pending),
    )
