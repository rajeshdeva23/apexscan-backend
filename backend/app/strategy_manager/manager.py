"""StrategyManager — evaluation routing (P5.3) + lifecycle/requirement orchestration (P5.4).

Orchestration only (docs/07 §6). The routing half (P5.3) subscribes to MarketContext
lifecycle events on the synchronous EventBus, routes each accepted context to the
RUNNING strategies whose trigger is relevant and whose requirements are satisfied,
invokes ``strategy.evaluate`` with per-strategy failure isolation, and collects
immutable evaluation records in deterministic (strategy_id-ascending) order. The
lifecycle half (P5.4) drives START/PAUSE/RESUME/STOP/FORCE STOP through the P5.2 FSM
and the :class:`RequirementsCoordinator`: START registers requirements, applies the
effective live-timeframe union to the Market-Engine seam, warms historical
requirements, and reaches RUNNING only when dependencies are ready (else ERROR,
requirements retained — ADR-007 D3/D7); STOP/FORCE STOP release requirements.

The publication half (P5.5) promotes each cycle's emittable evaluations to immutable
StrategyResults, applies emission-policy dedup, ranks the surviving matches, and
publishes a StrategyResultsPublished event on the same bus — only when at least one
result is material (ADR-007 D10/D11). Dedup state is retained across PAUSE/ERROR and
reset on STOP/FORCE STOP, following requirement retention (ADR-007 D4–D7).

It computes no market logic and mutates no MarketContext/registry/descriptor, owns no
persistence (durable StrategyResult storage is a later slice — docs/02 §6.4), and
never recomputes a strategy-owned score. Repeated evaluation failures move a strategy
to ERROR via the FSM without releasing requirements.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

from app.events.bus import EventBus, Subscription
from app.market_engine.context import MarketContext
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.schemas.market_data import Instrument
from app.strategies.configuration import StrategyConfiguration
from app.strategies.contracts import Strategy, StrategyEvaluationMetadata
from app.strategies.enums import EvaluationStatus, StrategyLifecycleState, StrategyTrigger
from app.strategies.registry import StrategyRegistry
from app.strategies.results import StrategyEvaluation, StrategyResult
from app.strategy_manager.dedup import EmissionDeduplicator
from app.strategy_manager.errors import StrategyLifecycleError
from app.strategy_manager.events import StrategyResultsPublished
from app.strategy_manager.lifecycle import StrategyLifecycle
from app.strategy_manager.promotion import promote_evaluation
from app.strategy_manager.ranking import rank_results
from app.strategy_manager.readiness import assess_readiness
from app.strategy_manager.records import Readiness, StrategyEvaluationRecord
from app.strategy_manager.requirements_bridge import RequirementsCoordinator
from app.strategy_manager.triggers import is_triggered

_EVALUATION_FAILED = (("error", "evaluation_failed"),)
# Only outcome states are externally emittable; SKIPPED/ERROR stay internal
# observations (docs/07 §11/§20; ADR-007 D10 governs emission over MATCH/NO_MATCH).
_EMITTABLE = frozenset({EvaluationStatus.MATCHED, EvaluationStatus.NO_MATCH})


class StrategyManager:
    """Routes MarketContext updates to RUNNING strategies with isolation and gating."""

    def __init__(
        self,
        *,
        registry: StrategyRegistry,
        lifecycle: StrategyLifecycle,
        configurations: Mapping[str, StrategyConfiguration],
        error_threshold: int,
        bus: EventBus,
        sink: Callable[[StrategyEvaluationRecord], None] | None = None,
        requirements: RequirementsCoordinator | None = None,
    ) -> None:
        """Wire the manager to the registry, lifecycle, config lookup, bus, and sink.

        Args:
            registry: The strategy registry (available plug-ins).
            lifecycle: The lifecycle FSM (runtime states) the manager reads and drives.
            configurations: Validated per-strategy configuration, keyed by strategy_id.
            error_threshold: Consecutive evaluation failures that move a strategy to
                ERROR (required; no default policy — docs/07 §20 leaves the number open).
            bus: The in-process EventBus to subscribe to.
            sink: Optional callback receiving each evaluation record for observation.
            requirements: The requirement-provisioning coordinator (P5.4). Required
                for the lifecycle commands (START/STOP/FORCE STOP); routing (P5.3)
                works without it.

        Raises:
            ValueError: If ``error_threshold`` is not positive.
        """
        if error_threshold < 1:
            raise ValueError("error_threshold must be a positive integer")
        self._registry = registry
        self._lifecycle = lifecycle
        self._configurations = configurations
        self._error_threshold = error_threshold
        self._bus = bus
        self._sink = sink
        self._requirements = requirements
        self._previous: dict[Instrument, MarketContext] = {}
        self._latest: dict[Instrument, tuple[StrategyEvaluationRecord, ...]] = {}
        self._error_counts: dict[str, int] = {}
        self._subscriptions: list[Subscription[Any]] = []
        self._dedup = EmissionDeduplicator()

    def subscribe(self) -> None:
        """Subscribe to MarketContext lifecycle events (idempotent)."""
        if self._subscriptions:
            return
        self._subscriptions = [
            self._bus.subscribe(MarketContextCreated, self._on_created),
            self._bus.subscribe(MarketContextUpdated, self._on_updated),
        ]

    def unsubscribe(self) -> None:
        """Stop receiving context events; no further evaluation occurs."""
        for subscription in self._subscriptions:
            self._bus.unsubscribe(subscription)
        self._subscriptions = []

    def evaluations_for(self, instrument: Instrument) -> tuple[StrategyEvaluationRecord, ...]:
        """Return the most recent cycle's evaluation records for an instrument."""
        return self._latest.get(instrument, ())

    # ----------------------------------------------------------------------- #
    # Lifecycle-command orchestration (P5.4; ADR-007 D3–D7)
    # ----------------------------------------------------------------------- #
    async def start(self, strategy_id: str, *, reference: datetime) -> StrategyLifecycleState:
        """Start (or restart) a strategy: register requirements, warm, reach RUNNING.

        Enters STARTING via the FSM, then validates configuration, registers the
        strategy's requirements, applies the effective live-timeframe union, and
        warms historical requirements. Reaches RUNNING only when dependencies are
        ready; otherwise ERROR — with the acquired requirements **retained** so the
        strategy can be diagnosed and restarted (ADR-007 D3/D7). Requirement
        registration is idempotent, so an ERROR→START restart never double-registers.

        Args:
            strategy_id: The strategy to start.
            reference: The deterministic reference instant for historical warmup.

        Returns:
            The resulting lifecycle state (RUNNING on success, ERROR on failure).
        """
        coordinator = self._require_coordinator()
        self._lifecycle.start(strategy_id)
        try:
            strategy = self._registry.get(strategy_id)
            self._require_valid_configuration(strategy_id, strategy)
            coordinator.register(strategy_id, strategy.requirements)
            coordinator.apply_live_union()
            ready = await coordinator.warm(strategy.requirements, reference=reference)
        except Exception:
            # Any startup fault isolates to this strategy; requirements stay registered.
            self._lifecycle.mark_error(strategy_id)
            return StrategyLifecycleState.ERROR
        if not ready:
            self._lifecycle.mark_error(strategy_id)
            return StrategyLifecycleState.ERROR
        return self._lifecycle.mark_running(strategy_id)

    def pause(self, strategy_id: str) -> StrategyLifecycleState:
        """Pause evaluation while retaining requirements and per-session state (D4)."""
        return self._lifecycle.pause(strategy_id)

    def resume(self, strategy_id: str) -> StrategyLifecycleState:
        """Resume a paused strategy without re-registering requirements or warming (D4)."""
        return self._lifecycle.resume(strategy_id)

    def stop(self, strategy_id: str) -> StrategyLifecycleState:
        """Stop a strategy, release its (unshared) requirements, and reset its dedup (D5).

        STOP tears down the strategy's session footprint: its emission/dedup state is
        reset, so a restart begins a fresh emission history (ADR-007 D5; D4 couples
        per-session state to requirement retention).
        """
        state = self._lifecycle.stop(strategy_id)
        self._require_coordinator().release(strategy_id)
        self._dedup.reset_strategy(strategy_id)
        return state

    def force_stop(self, strategy_id: str) -> StrategyLifecycleState:
        """Force-stop a RUNNING/PAUSED/ERROR strategy and release its requirements (D6).

        Uses the P5.2 default (``clean_cancellable=False``): a strategy mid-STARTING
        is never force-stopped, because P5.4 START is atomic — no startup work
        outlives the ``start`` call — so no dangling cancellable work exists to prove
        clean, and STARTING is never externally observable.
        """
        state = self._lifecycle.force_stop(strategy_id)
        self._require_coordinator().release(strategy_id)
        self._dedup.reset_strategy(strategy_id)
        return state

    def _require_coordinator(self) -> RequirementsCoordinator:
        if self._requirements is None:
            raise StrategyLifecycleError(
                "lifecycle commands require a RequirementsCoordinator (P5.4)"
            )
        return self._requirements

    def _require_valid_configuration(self, strategy_id: str, strategy: Strategy) -> None:
        """Reject a start without a configuration of the strategy's declared type.

        Runs before requirement registration (ADR-007 D3 order), so a config failure
        reaches ERROR having acquired no requirements.
        """
        configuration = self._configurations.get(strategy_id)
        if configuration is None or not isinstance(configuration, strategy.configuration_type):
            raise StrategyLifecycleError(
                f"strategy '{strategy_id}' has no valid configuration to start"
            )

    def _on_created(self, event: MarketContextCreated) -> None:
        self._handle(event.context)

    def _on_updated(self, event: MarketContextUpdated) -> None:
        self._handle(event.context)

    def _handle(self, context: MarketContext) -> None:
        previous = self._previous.get(context.instrument)
        if previous is not None and context.version <= previous.version:
            return  # stale/duplicate/out-of-order — never regress or reorder
        records = tuple(self._records_for(context, previous))
        self._previous[context.instrument] = context
        self._latest[context.instrument] = records
        if self._sink is not None:
            for record in records:
                self._sink(record)
        self._publish_results(context, records)

    def _records_for(
        self, context: MarketContext, previous: MarketContext | None
    ) -> list[StrategyEvaluationRecord]:
        records: list[StrategyEvaluationRecord] = []
        for strategy_id in self._running_ids():
            strategy = self._registry.get(strategy_id)
            trigger = strategy.requirements.trigger
            if not is_triggered(trigger, previous=previous, current=context):
                continue  # trigger mismatch — not considered, no record
            records.append(self._evaluate_one(strategy, trigger, context))
        return records

    # ----------------------------------------------------------------------- #
    # Result promotion, emission dedup, ranking, and publication (P5.5; D10/D11)
    # ----------------------------------------------------------------------- #
    def _publish_results(
        self, context: MarketContext, records: tuple[StrategyEvaluationRecord, ...]
    ) -> None:
        trading_date = context.session.trading_date if context.session is not None else None
        emitted = [
            result
            for record in records
            if (result := self._emit_candidate(record, context, trading_date)) is not None
        ]
        if not emitted:
            return  # nothing material this cycle — suppress the publication (ADR-007 D10)
        self._bus.publish(
            StrategyResultsPublished(
                instrument=context.instrument,
                context_version=context.version,
                results=tuple(emitted),
                ranked=rank_results(emitted),
                trading_date=trading_date,
            )
        )

    def _emit_candidate(
        self,
        record: StrategyEvaluationRecord,
        context: MarketContext,
        trading_date: date | None,
    ) -> StrategyResult | None:
        if record.evaluation.status not in _EMITTABLE:
            return None  # SKIPPED/ERROR stay internal (never promoted/published)
        configuration = self._configurations.get(record.strategy_id)
        if configuration is None:
            return None
        descriptor = self._registry.get(record.strategy_id).descriptor
        result = promote_evaluation(
            evaluation=record.evaluation,
            descriptor=descriptor,
            config_version=configuration.config_version,
            evaluation_timestamp=context.observed_at,
        )
        if not self._dedup.should_emit(
            result, policy=descriptor.emission_policy, trading_date=trading_date
        ):
            return None
        return result

    def _running_ids(self) -> tuple[str, ...]:
        running = {
            status.strategy_id
            for status in self._lifecycle.snapshot()
            if status.state is StrategyLifecycleState.RUNNING
        }
        return tuple(sorted(running & set(self._registry.identifiers())))

    def _evaluate_one(
        self, strategy: Strategy, trigger: StrategyTrigger, context: MarketContext
    ) -> StrategyEvaluationRecord:
        strategy_id = strategy.descriptor.strategy_id
        configuration = self._configurations.get(strategy_id)
        readiness = assess_readiness(
            descriptor=strategy.descriptor,
            requirements=strategy.requirements,
            configuration=configuration,
            configuration_type=strategy.configuration_type,
            context=context,
        )
        if readiness is not Readiness.READY or configuration is None:
            return self._record(strategy_id, trigger, readiness, self._skipped(context))
        return self._run(strategy, strategy_id, trigger, configuration, context)

    def _run(
        self,
        strategy: Strategy,
        strategy_id: str,
        trigger: StrategyTrigger,
        configuration: StrategyConfiguration,
        context: MarketContext,
    ) -> StrategyEvaluationRecord:
        metadata = self._metadata(trigger, context)
        try:
            evaluation = strategy.evaluate(context, configuration, metadata)
            self._require_consistent(evaluation, context)
        except Exception:
            # Isolate any strategy fault; the manager handler never poisons the pipeline.
            return self._fail(strategy_id, trigger, context)
        self._error_counts[strategy_id] = 0
        return self._record(strategy_id, trigger, Readiness.READY, evaluation)

    def _fail(
        self, strategy_id: str, trigger: StrategyTrigger, context: MarketContext
    ) -> StrategyEvaluationRecord:
        count = self._error_counts.get(strategy_id, 0) + 1
        self._error_counts[strategy_id] = count
        if count >= self._error_threshold:
            self._lifecycle.mark_error(strategy_id)
        return self._record(strategy_id, trigger, Readiness.READY, self._error(context))

    @staticmethod
    def _require_consistent(evaluation: StrategyEvaluation, context: MarketContext) -> None:
        if (
            evaluation.instrument != context.instrument
            or evaluation.context_version != context.version
        ):
            raise ValueError("strategy evaluation does not match the evaluated context")

    def _metadata(
        self, trigger: StrategyTrigger, context: MarketContext
    ) -> StrategyEvaluationMetadata:
        return StrategyEvaluationMetadata(
            trigger=trigger,
            context_version=context.version,
            observed_at=context.observed_at,
            trading_date=context.session.trading_date if context.session is not None else None,
        )

    @staticmethod
    def _skipped(context: MarketContext) -> StrategyEvaluation:
        return StrategyEvaluation(
            instrument=context.instrument,
            context_version=context.version,
            status=EvaluationStatus.SKIPPED,
        )

    @staticmethod
    def _error(context: MarketContext) -> StrategyEvaluation:
        return StrategyEvaluation(
            instrument=context.instrument,
            context_version=context.version,
            status=EvaluationStatus.ERROR,
            diagnostics=_EVALUATION_FAILED,
        )

    @staticmethod
    def _record(
        strategy_id: str,
        trigger: StrategyTrigger,
        readiness: Readiness,
        evaluation: StrategyEvaluation,
    ) -> StrategyEvaluationRecord:
        return StrategyEvaluationRecord(
            strategy_id=strategy_id, trigger=trigger, readiness=readiness, evaluation=evaluation
        )
