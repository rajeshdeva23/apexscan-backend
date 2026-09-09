"""Historical service: coverage, missing-day planning, idempotent backfill (DECOUPLING PHASE F).

Orchestrates the store, a provider-neutral source, and the trading calendar over the instruments of
an effective Phase-E UniverseSnapshot (callers pass ``snapshot.instruments``' canonical
``Instrument``s — this package never imports the universe package). It answers "does instrument X
have the required N trading days?" — never "is X ready for strategy Y?" (Phase G).

Fail closed: a source failure leaves stored data untouched and coverage incomplete (no fabricated
bars, no readiness claim); an ADJUSTED request fails when no corporate-action authority exists;
RAW data is always available. Backfill is idempotent and never silently overwrites a differing bar.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.market_engine.session import TradingCalendar
from app.market_history.coverage import (
    HistoricalCoverage,
    compute_coverage,
    previous_trading_date,
    required_trading_dates,
)
from app.market_history.diagnostics import HistoryDiagnostics, HistoryMetrics
from app.market_history.models import (
    HistoricalDailyBar,
    HistoryRequirement,
    PriceAdjustment,
)
from app.market_history.source import HistoricalDataSource, HistoricalSourceError
from app.market_history.store import HistoricalBarStore, UpsertOutcome
from app.schemas.market_data import Instrument


class AdjustedDataUnavailableError(RuntimeError):
    """Raised when adjusted history is requested but no corporate-action authority exists."""


class PreviousCloseComparison(StrEnum):
    """Result of comparing a historical close against the authoritative previous_close."""

    AGREE = "agree"
    DISAGREE = "disagree"
    UNAVAILABLE = "unavailable"


class InstrumentBackfillResult(BaseModel):
    """Per-instrument backfill outcome (returned from an explicit call, not global metrics)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    instrument_identity: str
    missing_before: tuple[date, ...]
    inserted: int
    unchanged: int
    conflicts: int
    fetch_ok: bool
    complete_after: bool


class BackfillReport(BaseModel):
    """Aggregate backfill result for a set of instruments."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    results: tuple[InstrumentBackfillResult, ...]

    @property
    def complete_count(self) -> int:
        """Number of instruments complete after backfill."""
        return sum(1 for result in self.results if result.complete_after)


def _identity(instrument: Instrument) -> str:
    return f"{instrument.exchange}:{instrument.symbol}"


class HistoricalService:
    """Broker-neutral historical coverage + backfill over UniverseSnapshot instruments."""

    def __init__(
        self,
        *,
        store: HistoricalBarStore,
        source: HistoricalDataSource,
        calendar: TradingCalendar,
        now: Callable[[], datetime],
        corporate_action_authority_available: bool = False,
    ) -> None:
        self._store = store
        self._source = source
        self._calendar = calendar
        self._now = now
        self._corporate_action_authority = corporate_action_authority_available
        self._metrics = HistoryMetrics()

    def coverage(
        self,
        instrument_identity: str,
        requirement: HistoryRequirement,
        *,
        as_of: date,
        adjustment: PriceAdjustment = PriceAdjustment.RAW,
    ) -> HistoricalCoverage:
        """Compute trading-calendar-aware coverage for one instrument."""
        self._guard_adjustment(adjustment)
        required = required_trading_dates(
            self._calendar, as_of=as_of, trading_days=requirement.trading_days
        )
        stored = self._store.available_dates(instrument_identity, adjustment=adjustment)
        coverage = compute_coverage(
            instrument_identity=instrument_identity, required_dates=required, stored_dates=stored
        )
        self._metrics.coverage_checks += 1
        self._metrics.missing_trading_days += len(coverage.missing_dates)
        if coverage.is_complete:
            self._metrics.complete_instruments += 1
        else:
            self._metrics.incomplete_instruments += 1
        return coverage

    def previous_trading_bar(
        self,
        instrument_identity: str,
        *,
        as_of: date,
        adjustment: PriceAdjustment = PriceAdjustment.RAW,
    ) -> HistoricalDailyBar | None:
        """Return the bar for the previous completed trading session before ``as_of`` (calendar)."""
        self._guard_adjustment(adjustment)
        previous = previous_trading_date(self._calendar, as_of)
        series = self._store.read_range(
            instrument_identity, previous, previous, adjustment=adjustment
        )
        return series.latest()

    def plan_missing(
        self,
        instruments: Iterable[Instrument],
        requirement: HistoryRequirement,
        *,
        as_of: date,
        adjustment: PriceAdjustment = PriceAdjustment.RAW,
    ) -> dict[str, tuple[date, ...]]:
        """Return ``identity -> missing trading dates`` for the given universe instruments."""
        self._guard_adjustment(adjustment)
        required = required_trading_dates(
            self._calendar, as_of=as_of, trading_days=requirement.trading_days
        )
        plan: dict[str, tuple[date, ...]] = {}
        for instrument in instruments:
            identity = _identity(instrument)
            stored = self._store.available_dates(identity, adjustment=adjustment)
            coverage = compute_coverage(
                instrument_identity=identity, required_dates=required, stored_dates=stored
            )
            plan[identity] = coverage.missing_dates
        return plan

    async def backfill(
        self,
        instruments: Iterable[Instrument],
        requirement: HistoryRequirement,
        *,
        as_of: date,
        adjustment: PriceAdjustment = PriceAdjustment.RAW,
    ) -> BackfillReport:
        """Fetch and idempotently store only the missing trading dates for each instrument."""
        self._guard_adjustment(adjustment)
        required = required_trading_dates(
            self._calendar, as_of=as_of, trading_days=requirement.trading_days
        )
        results = [
            await self._backfill_instrument(instrument, required, adjustment)
            for instrument in instruments
        ]
        return BackfillReport(results=tuple(results))

    async def _backfill_instrument(
        self,
        instrument: Instrument,
        required: tuple[date, ...],
        adjustment: PriceAdjustment,
    ) -> InstrumentBackfillResult:
        """Backfill one instrument's missing dates; fail-closed and idempotent."""
        identity = _identity(instrument)
        stored = self._store.available_dates(identity, adjustment=adjustment)
        coverage = compute_coverage(
            instrument_identity=identity, required_dates=required, stored_dates=stored
        )
        missing = coverage.missing_dates
        counts = {UpsertOutcome.INSERTED: 0, UpsertOutcome.UNCHANGED: 0, UpsertOutcome.CONFLICT: 0}
        fetch_ok = True
        if missing:
            fetch_ok = await self._fetch_and_store(instrument, missing, adjustment, counts)
        after = compute_coverage(
            instrument_identity=identity,
            required_dates=required,
            stored_dates=self._store.available_dates(identity, adjustment=adjustment),
        )
        return InstrumentBackfillResult(
            instrument_identity=identity,
            missing_before=missing,
            inserted=counts[UpsertOutcome.INSERTED],
            unchanged=counts[UpsertOutcome.UNCHANGED],
            conflicts=counts[UpsertOutcome.CONFLICT],
            fetch_ok=fetch_ok,
            complete_after=after.is_complete,
        )

    async def _fetch_and_store(
        self,
        instrument: Instrument,
        missing: tuple[date, ...],
        adjustment: PriceAdjustment,
        counts: dict[UpsertOutcome, int],
    ) -> bool:
        """Fetch the missing range and idempotently store it; return False on source failure."""
        self._metrics.fetch_attempts += 1
        self._metrics.last_fetch_at = self._now()
        try:
            fetched = await self._source.fetch_daily_bars(instrument, missing[0], missing[-1])
        except HistoricalSourceError:
            self._metrics.fetch_failures += 1
            self._metrics.last_failure_at = self._now()
            return False  # existing data untouched; coverage stays incomplete; nothing fabricated
        self._metrics.fetch_successes += 1
        self._metrics.last_success_at = self._now()
        self._metrics.bars_received += len(fetched)
        wanted = set(missing)
        outcomes = self._store.upsert_many(
            bar for bar in fetched if bar.trading_date in wanted and bar.adjustment is adjustment
        )
        for outcome, count in outcomes.items():
            counts[outcome] += count
        self._metrics.bars_inserted += outcomes.get(UpsertOutcome.INSERTED, 0)
        self._metrics.bars_unchanged += outcomes.get(UpsertOutcome.UNCHANGED, 0)
        self._metrics.bar_conflicts += outcomes.get(UpsertOutcome.CONFLICT, 0)
        return True

    def compare_previous_close(
        self,
        instrument_identity: str,
        *,
        as_of: date,
        reference_previous_close: object | None,
    ) -> PreviousCloseComparison:
        """Surface (never replace) any disagreement with the authoritative previous_close.

        Phase D's ``MarketReference.previous_close`` remains the authority; this only reports
        whether the historical previous session's close agrees, for audit.
        """
        if reference_previous_close is None:
            return PreviousCloseComparison.UNAVAILABLE
        bar = self.previous_trading_bar(instrument_identity, as_of=as_of)
        if bar is None:
            return PreviousCloseComparison.UNAVAILABLE
        return (
            PreviousCloseComparison.AGREE
            if bar.close == reference_previous_close
            else PreviousCloseComparison.DISAGREE
        )

    def diagnostics(self) -> HistoryDiagnostics:
        """Snapshot bounded historical diagnostics."""
        return self._metrics.snapshot()

    def _guard_adjustment(self, adjustment: PriceAdjustment) -> None:
        """Fail closed for adjusted requests when no corporate-action authority exists."""
        if adjustment is PriceAdjustment.ADJUSTED and not self._corporate_action_authority:
            raise AdjustedDataUnavailableError(
                "adjusted history requires a corporate-action authority, which is unavailable"
            )
