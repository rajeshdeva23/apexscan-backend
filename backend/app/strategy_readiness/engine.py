"""Deterministic per-(instrument, strategy) readiness engine (DECOUPLING PHASE G).

Composes Phase-E universe membership, Phase-F historical coverage, and Phase-D/live reference
state (all via narrow broker-neutral Protocols) against a strategy's declared
:class:`ReadinessRequirement`. Pure and synchronous; a :class:`Clock` is injected so identical
inputs always yield an identical result. Fails closed: if any required input is missing, stale,
mismatched, or unverifiable, the status is never READY.

Not wired into production: the engine and its ``StrategyReadinessGate`` seam are capability only;
production strategy routing/behaviour is unchanged. This does not touch the live evaluation-time
gate (``strategy_manager.assess_readiness``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.market_engine.clock import Clock
from app.market_history import AdjustedDataUnavailableError, HistoricalCoverage
from app.strategy_readiness.diagnostics import ReadinessDiagnostics, ReadinessMetrics
from app.strategy_readiness.models import (
    ReadinessReason,
    ReadinessReasonCode,
    StrategyReadinessResult,
    StrategyReadinessSnapshot,
    StrategyReadinessStatus,
)
from app.strategy_readiness.requirements import ReadinessRequirement


@runtime_checkable
class UniverseView(Protocol):
    """Effective-universe membership + current version (Phase-E backed)."""

    def is_member(self, instrument_identity: str) -> bool:
        """Whether the instrument is in the current effective universe."""
        ...

    def current_universe_version(self) -> int:
        """The backend's current effective universe version."""
        ...


@runtime_checkable
class HistoricalCoverageView(Protocol):
    """Historical completeness lookups (Phase-F backed)."""

    def coverage(
        self, instrument_identity: str, *, trading_days: int, adjustment: object, as_of: date
    ) -> HistoricalCoverage:
        """Coverage of the last ``trading_days`` on/before ``as_of`` (may raise if adjusted)."""
        ...

    def has_previous_trading_bar(self, instrument_identity: str, *, as_of: date) -> bool:
        """Whether the previous completed trading-day bar is stored."""
        ...


@runtime_checkable
class LiveReadinessState(Protocol):
    """Per-instrument canonical live/reference state (Phase-D + live backed)."""

    def previous_close(self, instrument_identity: str) -> Decimal | None:
        """Authoritative canonical previous close, or None."""
        ...

    def session_open(self, instrument_identity: str) -> Decimal | None:
        """Canonical current-session open, or None."""
        ...

    def last_price(self, instrument_identity: str) -> Decimal | None:
        """Latest canonical last-traded price, or None."""
        ...

    def observed_at(self, instrument_identity: str) -> datetime | None:
        """Timestamp of the most recent live observation, or None if never observed."""
        ...

    def data_universe_version(self, instrument_identity: str) -> int | None:
        """Universe version associated with this instrument's current data, or None."""
        ...


@runtime_checkable
class StrategyReadinessGate(Protocol):
    """Narrow routing seam (capability only; NOT wired into production composition)."""

    def can_route(self, instrument_identity: str, strategy_id: str) -> bool:
        """Whether the strategy may consider the instrument (READY)."""
        ...


class StrategyReadinessEngine:
    """Evaluates (instrument, strategy) readiness deterministically, failing closed."""

    def __init__(
        self,
        *,
        universe: UniverseView,
        coverage: HistoricalCoverageView,
        live: LiveReadinessState,
        clock: Clock,
    ) -> None:
        self._universe = universe
        self._coverage = coverage
        self._live = live
        self._clock = clock
        self._metrics = ReadinessMetrics()

    def evaluate(
        self,
        instrument_identity: str,
        strategy_id: str,
        requirement: ReadinessRequirement,
        *,
        trading_date: date,
    ) -> StrategyReadinessResult:
        """Evaluate one (instrument, strategy) pair, applying the frozen status precedence."""
        status, reasons = self._classify(instrument_identity, requirement, as_of=trading_date)
        result = StrategyReadinessResult(
            instrument_identity=instrument_identity,
            strategy_id=strategy_id,
            universe_version=self._universe.current_universe_version(),
            trading_date=trading_date,
            status=status,
            reasons=reasons,
            evaluated_at=self._clock.now(),
        )
        self._metrics.record(status, self._clock.now())
        return result

    def evaluate_batch(
        self,
        instrument_identities: Iterable[str],
        strategy_requirements: Mapping[str, ReadinessRequirement],
        *,
        trading_date: date,
    ) -> StrategyReadinessSnapshot:
        """Evaluate every (instrument, strategy) pair into an immutable snapshot."""
        results = tuple(
            self.evaluate(identity, strategy_id, requirement, trading_date=trading_date)
            for identity in instrument_identities
            for strategy_id, requirement in strategy_requirements.items()
        )
        return StrategyReadinessSnapshot(
            trading_date=trading_date,
            universe_version=self._universe.current_universe_version(),
            results=results,
        )

    def diagnostics(self) -> ReadinessDiagnostics:
        """Snapshot bounded cumulative readiness diagnostics."""
        return self._metrics.snapshot()

    def _classify(
        self, identity: str, requirement: ReadinessRequirement, *, as_of: date
    ) -> tuple[StrategyReadinessStatus, tuple[ReadinessReason, ...]]:
        """Return the single primary status + reasons per frozen precedence (fail closed)."""
        if not self._universe.is_member(identity):
            return StrategyReadinessStatus.REMOVED, (
                ReadinessReason(code=ReadinessReasonCode.REMOVED_FROM_UNIVERSE, field="universe"),
            )

        mismatch = self._universe_mismatch(identity)
        if mismatch is not None:
            return StrategyReadinessStatus.UNIVERSE_MISMATCH, (mismatch,)

        history_problem = self._history_problem(identity, requirement, as_of=as_of)
        if history_problem is not None:
            return StrategyReadinessStatus.MISSING_HISTORY, (history_problem,)

        if requirement.needs_previous_close and self._live.previous_close(identity) is None:
            return StrategyReadinessStatus.MISSING_REFERENCE, (
                ReadinessReason(
                    code=ReadinessReasonCode.MISSING_PREVIOUS_CLOSE, field="previous_close"
                ),
            )

        return self._live_status(identity, requirement)

    def _universe_mismatch(self, identity: str) -> ReadinessReason | None:
        """Detect data associated with a different universe version than the backend's current."""
        data_version = self._live.data_universe_version(identity)
        current = self._universe.current_universe_version()
        if data_version is not None and data_version != current:
            return ReadinessReason(
                code=ReadinessReasonCode.UNIVERSE_VERSION_MISMATCH,
                field="universe_version",
                required=str(current),
                available=str(data_version),
            )
        return None

    def _history_problem(
        self, identity: str, requirement: ReadinessRequirement, *, as_of: date
    ) -> ReadinessReason | None:
        """Return a MISSING_HISTORY reason if historical/previous-day needs are unmet."""
        if requirement.historical is not None:
            need = requirement.historical
            try:
                coverage = self._coverage.coverage(
                    identity,
                    trading_days=need.trading_days,
                    adjustment=need.adjustment,
                    as_of=as_of,
                )
            except AdjustedDataUnavailableError:
                return ReadinessReason(
                    code=ReadinessReasonCode.ADJUSTED_HISTORY_UNAVAILABLE,
                    field="historical.adjustment",
                    required=str(need.adjustment),
                )
            if not coverage.is_complete:
                return ReadinessReason(
                    code=ReadinessReasonCode.INSUFFICIENT_HISTORY,
                    field="historical.trading_days",
                    required=str(need.trading_days),
                    available=str(coverage.available_count),
                )
        if requirement.needs_previous_day_ohlc and not self._coverage.has_previous_trading_bar(
            identity, as_of=as_of
        ):
            return ReadinessReason(
                code=ReadinessReasonCode.PREVIOUS_DAY_MISSING, field="previous_day_ohlc"
            )
        return None

    def _live_status(
        self, identity: str, requirement: ReadinessRequirement
    ) -> tuple[StrategyReadinessStatus, tuple[ReadinessReason, ...]]:
        """Resolve the live portion: WARMING_UP / MISSING_LIVE_DATA / STALE / READY."""
        if not requirement.requires_live:
            return StrategyReadinessStatus.READY, ()

        observed_at = self._live.observed_at(identity)
        if observed_at is None:  # no live observation yet -> not definitively missing (see §16)
            return StrategyReadinessStatus.WARMING_UP, (
                ReadinessReason(
                    code=ReadinessReasonCode.AWAITING_FIRST_OBSERVATION, field="observed_at"
                ),
            )

        if requirement.needs_session_open and self._live.session_open(identity) is None:
            return StrategyReadinessStatus.MISSING_LIVE_DATA, (
                ReadinessReason(
                    code=ReadinessReasonCode.MISSING_SESSION_OPEN, field="session_open"
                ),
            )
        if requirement.needs_last_price and self._live.last_price(identity) is None:
            return StrategyReadinessStatus.MISSING_LIVE_DATA, (
                ReadinessReason(code=ReadinessReasonCode.MISSING_LAST_PRICE, field="last_price"),
            )

        stale = self._staleness(observed_at, requirement)
        if stale is not None:
            return StrategyReadinessStatus.STALE, (stale,)
        return StrategyReadinessStatus.READY, ()

    def _staleness(
        self, observed_at: datetime, requirement: ReadinessRequirement
    ) -> ReadinessReason | None:
        """Return a STALE reason if the latest observation exceeds the declared max age."""
        if requirement.max_live_age is None:
            return None
        age = self._clock.now() - observed_at
        if age > requirement.max_live_age:
            return ReadinessReason(
                code=ReadinessReasonCode.LIVE_DATA_STALE,
                field="observed_at",
                required=str(requirement.max_live_age),
                available=str(age),
            )
        return None
