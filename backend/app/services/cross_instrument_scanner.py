"""Generic cross-instrument strategy scanner (ADR-012).

A broker-neutral, event-driven aggregator that turns per-instrument
``StrategyResultsPublished`` events into a deterministic, cross-instrument **ranked
snapshot per strategy**. It owns collection, eligibility, ordering, tie-breaking, and
bounded snapshot state — never any strategy mathematics (a strategy owns its metric).
Ranking is driven entirely by a registered :class:`ScannerRankingPolicy`
(``metric_name`` + ``ordering``), so any metric-emitting strategy is scannable with no
scanner code change and no ``if strategy_id == …`` branch (ADR-012 NCRS1/NCRS4/NCRS21).

The scanner is a passive ``EventBus`` subscriber (no task, no polling, no provider or
historical calls, no Market-Engine mutation; NCRS2/NCRS3/NCRS16/NCRS17). It keeps only
the current snapshot per strategy (bounded; NCRS14), reads the authoritative
``trading_date`` from the event (never a clock/calendar; NCRS9), and is non-directional
(rank #1 = narrowest/first by the governed metric, never a buy/sell signal; NCRS23).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.events.bus import EventBus, Subscription
from app.schemas.market_data import Instrument
from app.strategies.enums import EvaluationStatus
from app.strategies.results import StrategyResult
from app.strategy_manager.events import StrategyResultsPublished


class ScannerOrdering(StrEnum):
    """The direction a strategy's ranking metric is ordered in."""

    ASCENDING = "ascending"
    DESCENDING = "descending"


class ScannerSnapshotCompleteness(StrEnum):
    """Whether every expected instrument has reported a terminal published result."""

    PARTIAL = "partial"
    COMPLETE = "complete"


class ScannerPolicyConflictError(ValueError):
    """Raised when a conflicting ranking policy is registered for a strategy id."""


@dataclass(frozen=True, slots=True)
class ScannerRankingPolicy:
    """How the scanner ranks one strategy's cross-instrument results.

    Attributes:
        strategy_id: The strategy this policy applies to.
        metric_name: The ``MetricEntry`` name carrying the ranking value.
        ordering: Whether a smaller (ASCENDING) or larger (DESCENDING) metric ranks first.
    """

    strategy_id: str
    metric_name: str
    ordering: ScannerOrdering

    def __post_init__(self) -> None:
        """Reject empty identifiers, failing fast at construction."""
        if not self.strategy_id.strip():
            raise ScannerPolicyConflictError("ranking policy requires a non-empty strategy_id")
        if not self.metric_name.strip():
            raise ScannerPolicyConflictError("ranking policy requires a non-empty metric_name")


@dataclass(frozen=True, slots=True)
class ScannerCandidate:
    """One ranked, eligible instrument in a scanner snapshot (non-directional)."""

    instrument: Instrument
    strategy_id: str
    strategy_version: str
    config_version: str
    status: EvaluationStatus
    ranking_metric_name: str
    ranking_metric_value: Decimal
    rank: int


@dataclass(frozen=True, slots=True)
class ScannerSnapshot:
    """An immutable, deterministic cross-instrument ranked snapshot for one strategy."""

    strategy_id: str
    strategy_version: str
    config_version: str
    trading_date: date
    expected_count: int
    evaluated_count: int
    eligible_count: int
    completeness: ScannerSnapshotCompleteness
    candidates: tuple[ScannerCandidate, ...]


class ScannerRankingPolicyRegistry:
    """A bounded registry mapping ``strategy_id`` to its :class:`ScannerRankingPolicy`."""

    def __init__(self, policies: Sequence[ScannerRankingPolicy] = ()) -> None:
        """Build the registry, registering any seed policies (composition-owned)."""
        self._policies: dict[str, ScannerRankingPolicy] = {}
        for policy in policies:
            self.register(policy)

    def register(self, policy: ScannerRankingPolicy) -> None:
        """Register a policy; idempotent for an identical one, conflict fails closed."""
        existing = self._policies.get(policy.strategy_id)
        if existing is not None and existing != policy:
            raise ScannerPolicyConflictError(
                f"a different ranking policy is already registered for {policy.strategy_id!r}"
            )
        self._policies[policy.strategy_id] = policy

    def get(self, strategy_id: str) -> ScannerRankingPolicy | None:
        """Return the policy for ``strategy_id``, or ``None`` when not scanner-enabled."""
        return self._policies.get(strategy_id)

    def strategy_ids(self) -> tuple[str, ...]:
        """Return the scanner-enabled strategy ids in deterministic (sorted) order."""
        return tuple(sorted(self._policies))


@runtime_checkable
class ScannerSnapshotSource(Protocol):
    """The narrow read-only capability the transport layer consumes (ADR-012 API15).

    A broker-neutral view over the lifecycle-owned scanner: it reports whether a read is
    available (the runtime is composed and started), which strategies are scanner-enabled,
    and the current ranked snapshot for one strategy. It exposes no provider type, no engine,
    and no mutation — the API depends only on this contract.
    """

    def scanner_read_available(self) -> bool:
        """Return whether the scanner can be read (runtime composed and started/healthy)."""

    def scannable_strategy_ids(self) -> tuple[str, ...]:
        """Return the scanner-enabled strategy ids (deterministic order)."""

    def scanner_snapshot(self, strategy_id: str) -> ScannerSnapshot | None:
        """Return the current ranked snapshot for ``strategy_id``, or ``None`` when absent."""


@dataclass(slots=True)
class _CandidateRecord:
    """Mutable per-instrument slot inside a current snapshot (internal)."""

    instrument: Instrument
    status: EvaluationStatus
    metric_value: Decimal | None
    context_version: int

    @property
    def eligible(self) -> bool:
        """Return whether this record ranks (MATCHED with a valid metric)."""
        return self.status is EvaluationStatus.MATCHED and self.metric_value is not None


@dataclass(slots=True)
class _SnapshotState:
    """The current bounded snapshot state for one strategy (internal)."""

    trading_date: date
    strategy_version: str
    config_version: str
    records: dict[Instrument, _CandidateRecord] = field(default_factory=dict)


def _metric_value(result: StrategyResult, metric_name: str) -> Decimal | None:
    """Extract the named ranking metric as a Decimal, or ``None`` when missing/invalid."""
    for entry in result.metrics:
        if entry.name == metric_name:
            return entry.value if isinstance(entry.value, Decimal) else None
    return None


def _instrument_key(instrument: Instrument) -> tuple[str, str]:
    """Return the canonical ascending tie-break key for an instrument."""
    return (instrument.exchange, instrument.symbol)


class CrossInstrumentStrategyScanner:
    """Aggregate per-instrument strategy results into a ranked snapshot per strategy."""

    def __init__(
        self,
        *,
        instruments: Sequence[Instrument],
        policies: ScannerRankingPolicyRegistry,
        bus: EventBus,
    ) -> None:
        """Wire the scanner to its universe, ranking policies, and the shared event bus.

        Args:
            instruments: The canonical runtime universe; establishes ``expected_count``.
            policies: The composition-owned ranking-policy registry.
            bus: The shared in-process event bus the scanner subscribes to.
        """
        self._universe = frozenset(instruments)
        self._expected_count = len(self._universe)
        self._policies = policies
        self._bus = bus
        self._snapshots: dict[str, _SnapshotState] = {}
        self._subscription: Subscription[StrategyResultsPublished] | None = None

    def scannable_strategy_ids(self) -> tuple[str, ...]:
        """Return the scanner-enabled strategy ids (from the ranking-policy registry)."""
        return self._policies.strategy_ids()

    def subscribe(self) -> None:
        """Subscribe to ``StrategyResultsPublished`` (idempotent)."""
        if self._subscription is None:
            self._subscription = self._bus.subscribe(StrategyResultsPublished, self._handle)

    def unsubscribe(self) -> None:
        """Unsubscribe from the bus (idempotent, no leak)."""
        if self._subscription is not None:
            self._bus.unsubscribe(self._subscription)
            self._subscription = None

    def _handle(self, event: StrategyResultsPublished) -> None:
        """Ingest one publication cycle's results; a ``None`` trading date is ignored."""
        if event.trading_date is None:
            return  # cannot establish an honest snapshot identity — fail closed
        for result in event.results:
            self._ingest(result, event.trading_date)

    def _ingest(self, result: StrategyResult, trading_date: date) -> None:
        """Route one result into its strategy's current snapshot (fail-closed guards)."""
        policy = self._policies.get(result.strategy_id)
        if policy is None or result.instrument not in self._universe:
            return  # not scanner-enabled, or outside the configured universe
        if result.status not in (EvaluationStatus.MATCHED, EvaluationStatus.NO_MATCH):
            return
        state = self._resolve_state(result, trading_date)
        if state is None:
            return  # stale trading date or an identity conflict on the same date
        value = (
            _metric_value(result, policy.metric_name)
            if result.status is EvaluationStatus.MATCHED
            else None
        )
        self._store(state, result, value)

    def _resolve_state(self, result: StrategyResult, trading_date: date) -> _SnapshotState | None:
        """Advance/replace the strategy's current snapshot by trading date (bounded state)."""
        current = self._snapshots.get(result.strategy_id)
        if current is None or trading_date > current.trading_date:
            fresh = _SnapshotState(
                trading_date=trading_date,
                strategy_version=result.strategy_version,
                config_version=result.config_version,
            )
            self._snapshots[result.strategy_id] = fresh
            return fresh
        if trading_date < current.trading_date:
            return None  # stale — never mixes an earlier day into the current snapshot
        if (result.strategy_version, result.config_version) != (
            current.strategy_version,
            current.config_version,
        ):
            return None  # same-day version/config conflict — fail closed
        return current

    @staticmethod
    def _store(state: _SnapshotState, result: StrategyResult, value: Decimal | None) -> None:
        """Store/replace one instrument's slot (higher context_version wins; ties idempotent)."""
        existing = state.records.get(result.instrument)
        if existing is not None and result.context_version < existing.context_version:
            return  # older evaluation — ignore
        if existing is not None and result.context_version == existing.context_version:
            if existing.status is result.status and existing.metric_value == value:
                return  # identical re-delivery — idempotent
            return  # same-version conflict — keep the first (deterministic, fail closed)
        state.records[result.instrument] = _CandidateRecord(
            instrument=result.instrument,
            status=result.status,
            metric_value=value,
            context_version=result.context_version,
        )

    def snapshot(self, strategy_id: str) -> ScannerSnapshot | None:
        """Return the current deterministic ranked snapshot for ``strategy_id``, or ``None``."""
        state = self._snapshots.get(strategy_id)
        policy = self._policies.get(strategy_id)
        if state is None or policy is None:
            return None
        eligible = [record for record in state.records.values() if record.eligible]
        ranked = self._rank(eligible, policy)
        candidates = tuple(
            ScannerCandidate(
                instrument=record.instrument,
                strategy_id=strategy_id,
                strategy_version=state.strategy_version,
                config_version=state.config_version,
                status=EvaluationStatus.MATCHED,
                ranking_metric_name=policy.metric_name,
                ranking_metric_value=_require_value(record.metric_value),
                rank=index + 1,
            )
            for index, record in enumerate(ranked)
        )
        evaluated = len(state.records)
        completeness = (
            ScannerSnapshotCompleteness.COMPLETE
            if evaluated == self._expected_count
            else ScannerSnapshotCompleteness.PARTIAL
        )
        return ScannerSnapshot(
            strategy_id=strategy_id,
            strategy_version=state.strategy_version,
            config_version=state.config_version,
            trading_date=state.trading_date,
            expected_count=self._expected_count,
            evaluated_count=evaluated,
            eligible_count=len(candidates),
            completeness=completeness,
            candidates=candidates,
        )

    @staticmethod
    def _rank(
        records: list[_CandidateRecord], policy: ScannerRankingPolicy
    ) -> list[_CandidateRecord]:
        """Order eligible records by the metric (per ordering), tie-break instrument ascending."""
        descending = policy.ordering is ScannerOrdering.DESCENDING

        def _key(record: _CandidateRecord) -> tuple[Decimal, str, str]:
            value = _require_value(record.metric_value)
            return (-value if descending else value, *_instrument_key(record.instrument))

        return sorted(records, key=_key)


def _require_value(value: Decimal | None) -> Decimal:
    """Return a present metric value (eligible records always carry one)."""
    if value is None:  # unreachable: eligibility already requires a value
        raise ScannerPolicyConflictError("eligible candidate is missing its ranking metric")
    return value
