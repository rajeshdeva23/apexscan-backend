"""StrategyManager evaluation routing: triggers, readiness, isolation (P5.3).

Behaviour under test (docs/07 §6): the manager subscribes to MarketContext events,
routes each accepted context to RUNNING strategies whose trigger fired and whose
requirements are met, evaluates them in deterministic order with per-strategy
failure isolation, and drives repeated failures to ERROR via the P5.2 lifecycle.
These tests cover the routing/record half only; result promotion, dedup, ranking,
and publication (P5.5) are exercised in the dedicated P5.5 test modules.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.events.bus import EventBus
from app.market_engine.context import MarketContext, MarketState
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.strategies.enums import (
    EvaluationStatus,
    FactNeed,
    StrategyLifecycleState,
    StrategyTrigger,
)
from app.strategies.registry import StrategyRegistry
from app.strategies.results import StrategyEvaluation
from app.strategy_manager.lifecycle import StrategyLifecycle
from app.strategy_manager.manager import StrategyManager
from app.strategy_manager.records import Readiness, StrategyEvaluationRecord
from tests.unit.strategy_manager import builders as b


def _wire(
    strategies: list[b.FakeStrategy],
    *,
    lifecycle: StrategyLifecycle | None = None,
    error_threshold: int = 3,
    configs: dict | None = None,
    sink=None,
) -> tuple[StrategyManager, EventBus, StrategyLifecycle]:
    """Build a subscribed manager over the given strategies and lifecycle."""
    registry = StrategyRegistry()
    for strategy in strategies:
        registry.register(strategy)
    ids = [s.descriptor.strategy_id for s in strategies]
    lifecycle = lifecycle if lifecycle is not None else b.running_lifecycle(*ids)
    configs = configs if configs is not None else b.default_configs(*ids)
    bus = EventBus()
    manager = StrategyManager(
        registry=registry,
        lifecycle=lifecycle,
        configurations=configs,
        error_threshold=error_threshold,
        bus=bus,
        sink=sink,
    )
    manager.subscribe()
    return manager, bus, lifecycle


def _publish(bus: EventBus, context: MarketContext, *, previous: int | None = None) -> None:
    """Publish a created (version 1) or updated context on the bus."""
    if context.version == 1 and previous is None:
        bus.publish(MarketContextCreated(context=context))
    else:
        bus.publish(MarketContextUpdated(context=context, previous_version=previous or 0))


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #
def test_error_threshold_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        StrategyManager(
            registry=StrategyRegistry(),
            lifecycle=StrategyLifecycle(),
            configurations={},
            error_threshold=0,
            bus=EventBus(),
        )


# --------------------------------------------------------------------------- #
# RUNNING-only routing and deterministic ordering
# --------------------------------------------------------------------------- #
def test_only_running_strategies_are_evaluated() -> None:
    lifecycle = StrategyLifecycle()
    lifecycle.register("registered_only")  # stays REGISTERED
    for running in ("run_a", "run_b"):
        lifecycle.register(running)
        lifecycle.start(running)
        lifecycle.mark_running(running)
    lifecycle.register("paused")
    lifecycle.start("paused")
    lifecycle.mark_running("paused")
    lifecycle.pause("paused")

    strategies = [
        b.FakeStrategy("registered_only"),
        b.FakeStrategy("run_a"),
        b.FakeStrategy("run_b"),
        b.FakeStrategy("paused"),
    ]
    manager, bus, _ = _wire(strategies, lifecycle=lifecycle)
    _publish(bus, b.make_context())

    evaluated = [record.strategy_id for record in manager.evaluations_for(b.INSTRUMENT)]
    assert evaluated == ["run_a", "run_b"]


def test_evaluation_order_is_strategy_id_ascending_regardless_of_registration() -> None:
    strategies = [b.FakeStrategy("charlie"), b.FakeStrategy("alpha"), b.FakeStrategy("bravo")]
    manager, bus, _ = _wire(strategies)
    _publish(bus, b.make_context())

    evaluated = [record.strategy_id for record in manager.evaluations_for(b.INSTRUMENT)]
    assert evaluated == ["alpha", "bravo", "charlie"]


def test_running_strategy_absent_from_registry_is_skipped() -> None:
    # lifecycle tracks a RUNNING id the registry does not know — it is never invoked.
    lifecycle = b.running_lifecycle("known", "ghost")
    manager, bus, _ = _wire([b.FakeStrategy("known")], lifecycle=lifecycle)
    _publish(bus, b.make_context())

    evaluated = [record.strategy_id for record in manager.evaluations_for(b.INSTRUMENT)]
    assert evaluated == ["known"]


# --------------------------------------------------------------------------- #
# Trigger relevance (all six triggers)
# --------------------------------------------------------------------------- #
def test_on_context_always_fires() -> None:
    strategy = b.FakeStrategy("s", reqs=b.requirements(trigger=StrategyTrigger.ON_CONTEXT))
    manager, bus, _ = _wire([strategy])
    _publish(bus, b.make_context(version=1))
    _publish(bus, b.make_context(version=2), previous=1)
    assert len(strategy.calls) == 2


def test_on_tick_fires_only_on_a_tick_change() -> None:
    strategy = b.FakeStrategy(
        "s",
        reqs=b.requirements(trigger=StrategyTrigger.ON_TICK, fact_needs=(FactNeed.LATEST_TICK,)),
    )
    manager, bus, _ = _wire([strategy])
    _publish(bus, b.make_context(version=1, latest_tick=b.make_tick("100")))
    _publish(bus, b.make_context(version=2, latest_tick=b.make_tick("100")), previous=1)  # same
    _publish(bus, b.make_context(version=3, latest_tick=b.make_tick("101")), previous=2)  # changed

    triggered = [r.strategy_id for r in manager.evaluations_for(b.INSTRUMENT)]
    assert len(strategy.calls) == 2  # v1 (first appearance) and v3 (changed), not v2
    assert triggered == ["s"]  # only the last cycle's records are retained


def test_on_quote_fires_only_on_a_quote_change() -> None:
    strategy = b.FakeStrategy(
        "s",
        reqs=b.requirements(trigger=StrategyTrigger.ON_QUOTE, fact_needs=(FactNeed.LATEST_QUOTE,)),
    )
    manager, bus, _ = _wire([strategy])
    _publish(bus, b.make_context(version=1, latest_quote=b.make_quote("100", "101")))
    _publish(bus, b.make_context(version=2, latest_quote=b.make_quote("100", "101")), previous=1)
    _publish(bus, b.make_context(version=3, latest_quote=b.make_quote("100", "102")), previous=2)
    assert len(strategy.calls) == 2


def test_on_candle_finalized_fires_on_a_new_finalized_candle() -> None:
    strategy = b.FakeStrategy(
        "s",
        reqs=b.requirements(trigger=StrategyTrigger.ON_CANDLE_FINALIZED),
    )
    manager, bus, _ = _wire([strategy])
    one = (b.make_candle(0),)
    two = (b.make_candle(0), b.make_candle(1))
    _publish(bus, b.make_context(version=1, candle_sets=(b.candle_set(finalized=one),)))
    _publish(bus, b.make_context(version=2, candle_sets=(b.candle_set(finalized=one),)), previous=1)
    _publish(bus, b.make_context(version=3, candle_sets=(b.candle_set(finalized=two),)), previous=2)
    assert len(strategy.calls) == 2  # v1 (first candle) and v3 (new candle), not v2


def test_on_session_transition_fires_only_on_market_state_change() -> None:
    strategy = b.FakeStrategy(
        "s",
        reqs=b.requirements(trigger=StrategyTrigger.ON_SESSION_TRANSITION),
    )
    manager, bus, _ = _wire([strategy])
    pre = b.make_session(MarketState.PRE_OPEN)
    live = b.make_session(MarketState.LIVE_SESSION)
    _publish(bus, b.make_context(version=1, session=pre))  # first context: no transition
    _publish(bus, b.make_context(version=2, session=pre), previous=1)  # unchanged
    _publish(bus, b.make_context(version=3, session=live), previous=2)  # transition
    assert len(strategy.calls) == 1


def test_on_historical_ready_fires_on_none_to_installed_transition() -> None:
    strategy = b.FakeStrategy(
        "s",
        reqs=b.requirements(trigger=StrategyTrigger.ON_HISTORICAL_READY),
    )
    manager, bus, _ = _wire([strategy])
    hist = b.make_historical(lookback=1)
    _publish(bus, b.make_context(version=1))  # no history yet
    _publish(bus, b.make_context(version=2, historical=hist), previous=1)  # installed
    _publish(bus, b.make_context(version=3, historical=hist), previous=2)  # still present
    assert len(strategy.calls) == 1


# --------------------------------------------------------------------------- #
# Readiness gating
# --------------------------------------------------------------------------- #
def _record_for(manager: StrategyManager, strategy_id: str) -> StrategyEvaluationRecord:
    return next(r for r in manager.evaluations_for(b.INSTRUMENT) if r.strategy_id == strategy_id)


def test_ready_strategy_is_evaluated() -> None:
    strategy = b.FakeStrategy("s", behavior="match")
    manager, bus, _ = _wire([strategy])
    _publish(bus, b.make_context())
    record = _record_for(manager, "s")
    assert record.readiness is Readiness.READY
    assert record.evaluation.status is EvaluationStatus.MATCHED
    assert len(strategy.calls) == 1


def test_missing_facts_skips_without_invoking_the_strategy() -> None:
    strategy = b.FakeStrategy("s", reqs=b.requirements(fact_needs=(FactNeed.LATEST_TICK,)))
    manager, bus, _ = _wire([strategy])
    _publish(bus, b.make_context())  # no tick present
    record = _record_for(manager, "s")
    assert record.readiness is Readiness.MISSING_FACTS
    assert record.evaluation.status is EvaluationStatus.SKIPPED
    assert strategy.calls == []


def test_missing_historical_skips() -> None:
    strategy = b.FakeStrategy("s", reqs=b.requirements(historical=(b.historical_requirement(3),)))
    manager, bus, _ = _wire([strategy])
    _publish(bus, b.make_context(historical=b.make_historical(lookback=1)))  # too few
    record = _record_for(manager, "s")
    assert record.readiness is Readiness.MISSING_HISTORICAL
    assert strategy.calls == []


def test_missing_live_timeframe_skips_when_no_authoritative_candle() -> None:
    strategy = b.FakeStrategy("s", reqs=b.requirements(live_timeframes=(b.ONE_MINUTE,)))
    manager, bus, _ = _wire([strategy])
    _publish(bus, b.make_context())  # no candle set at all
    assert _record_for(manager, "s").readiness is Readiness.MISSING_LIVE_TIMEFRAME


def test_partial_only_candle_is_not_authoritative() -> None:
    strategy = b.FakeStrategy("s", reqs=b.requirements(live_timeframes=(b.ONE_MINUTE,)))
    manager, bus, _ = _wire([strategy])
    empty_set = b.candle_set(finalized=())  # timeframe present, no finalized candle
    _publish(bus, b.make_context(candle_sets=(empty_set,)))
    assert _record_for(manager, "s").readiness is Readiness.MISSING_LIVE_TIMEFRAME


def test_live_timeframe_ready_with_a_finalized_candle() -> None:
    strategy = b.FakeStrategy("s", reqs=b.requirements(live_timeframes=(b.ONE_MINUTE,)))
    manager, bus, _ = _wire([strategy])
    ready_set = b.candle_set(finalized=(b.make_candle(0),))
    _publish(bus, b.make_context(candle_sets=(ready_set,)))
    assert _record_for(manager, "s").readiness is Readiness.READY


def test_incompatible_context_version_skips() -> None:
    strategy = b.FakeStrategy("s", min_context_version=5)
    manager, bus, _ = _wire([strategy])
    _publish(bus, b.make_context(version=1))
    assert _record_for(manager, "s").readiness is Readiness.INCOMPATIBLE_CONTEXT
    assert strategy.calls == []


def test_missing_configuration_skips() -> None:
    strategy = b.FakeStrategy("s")
    manager, bus, _ = _wire([strategy], configs={})  # no configuration supplied
    _publish(bus, b.make_context())
    assert _record_for(manager, "s").readiness is Readiness.MISSING_CONFIGURATION
    assert strategy.calls == []


def test_wrong_configuration_type_skips() -> None:
    strategy = b.FakeStrategy("s", configuration_type=b.FakeConfig)  # expects FakeConfig
    # supply a base StrategyConfiguration, which is not a FakeConfig instance
    manager, bus, _ = _wire([strategy])
    _publish(bus, b.make_context())
    assert _record_for(manager, "s").readiness is Readiness.MISSING_CONFIGURATION


# --------------------------------------------------------------------------- #
# Failure isolation and the error threshold
# --------------------------------------------------------------------------- #
def test_one_failing_strategy_does_not_poison_the_others() -> None:
    good = b.FakeStrategy("aaa_ok", behavior="match")
    bad = b.FakeStrategy("bbb_boom", behavior="raise")
    plain = b.FakeStrategy("ccc_nomatch", behavior="no_match")
    manager, bus, _ = _wire([good, bad, plain])
    _publish(bus, b.make_context())

    by_id = {r.strategy_id: r for r in manager.evaluations_for(b.INSTRUMENT)}
    assert by_id["aaa_ok"].evaluation.status is EvaluationStatus.MATCHED
    assert by_id["bbb_boom"].evaluation.status is EvaluationStatus.ERROR
    assert by_id["bbb_boom"].evaluation.diagnostics == (("error", "evaluation_failed"),)
    assert by_id["ccc_nomatch"].evaluation.status is EvaluationStatus.NO_MATCH


def test_inconsistent_evaluation_is_treated_as_a_failure() -> None:
    strategy = b.FakeStrategy("s", behavior="inconsistent")
    manager, bus, _ = _wire([strategy])
    _publish(bus, b.make_context())
    assert _record_for(manager, "s").evaluation.status is EvaluationStatus.ERROR


def test_consecutive_failures_reach_the_threshold_and_mark_error() -> None:
    strategy = b.FakeStrategy("s", behavior="raise")
    manager, bus, lifecycle = _wire([strategy], error_threshold=3)
    for version in (1, 2, 3):
        _publish(bus, b.make_context(version=version), previous=version - 1)
    assert lifecycle.state_of("s") is StrategyLifecycleState.ERROR
    # once ERROR (no longer RUNNING) the strategy is not evaluated again
    calls_at_error = len(strategy.calls)
    _publish(bus, b.make_context(version=4), previous=3)
    assert len(strategy.calls) == calls_at_error
    assert manager.evaluations_for(b.INSTRUMENT) == ()


def test_below_threshold_does_not_mark_error() -> None:
    strategy = b.FakeStrategy("s", behavior="raise")
    manager, bus, lifecycle = _wire([strategy], error_threshold=3)
    for version in (1, 2):
        _publish(bus, b.make_context(version=version), previous=version - 1)
    assert lifecycle.state_of("s") is StrategyLifecycleState.RUNNING


class _FlakyStrategy(b.FakeStrategy):
    """A fake whose per-call outcome is scripted, to test failure-count resets."""

    def __init__(self, strategy_id: str, outcomes: list[str]) -> None:
        super().__init__(strategy_id)
        self._outcomes = outcomes

    def evaluate(self, context, configuration, metadata):  # type: ignore[no-untyped-def]
        self.calls.append(context)
        if self._outcomes[len(self.calls) - 1] == "raise":
            raise RuntimeError("scripted failure")
        return StrategyEvaluation(
            instrument=context.instrument,
            context_version=context.version,
            status=EvaluationStatus.NO_MATCH,
        )


def test_a_success_resets_the_consecutive_failure_count() -> None:
    # fail, fail, succeed, fail, fail — never three failures in a row
    strategy = _FlakyStrategy("s", ["raise", "raise", "ok", "raise", "raise"])
    manager, bus, lifecycle = _wire([strategy], error_threshold=3)
    for version in range(1, 6):
        _publish(bus, b.make_context(version=version), previous=version - 1)
    assert lifecycle.state_of("s") is StrategyLifecycleState.RUNNING


# --------------------------------------------------------------------------- #
# Context ordering / stale guard
# --------------------------------------------------------------------------- #
def test_stale_or_out_of_order_context_is_ignored() -> None:
    strategy = b.FakeStrategy("s", behavior="match")
    manager, bus, _ = _wire([strategy])
    _publish(bus, b.make_context(version=5), previous=4)
    _publish(bus, b.make_context(version=3), previous=2)  # older — must be ignored
    _publish(bus, b.make_context(version=5), previous=4)  # duplicate — must be ignored
    assert len(strategy.calls) == 1


def test_a_higher_version_advances_the_previous_context() -> None:
    strategy = b.FakeStrategy("s", reqs=b.requirements(trigger=StrategyTrigger.ON_CONTEXT))
    manager, bus, _ = _wire([strategy])
    _publish(bus, b.make_context(version=1))
    _publish(bus, b.make_context(version=2), previous=1)
    _publish(bus, b.make_context(version=3), previous=2)
    assert len(strategy.calls) == 3


# --------------------------------------------------------------------------- #
# Multi-instrument isolation
# --------------------------------------------------------------------------- #
def test_instruments_are_isolated() -> None:
    strategy = b.FakeStrategy("s", behavior="match")
    manager, bus, _ = _wire([strategy])
    _publish(bus, b.make_context(version=1, instrument=b.INSTRUMENT))
    _publish(bus, b.make_context(version=1, instrument=b.OTHER_INSTRUMENT))

    for instrument in (b.INSTRUMENT, b.OTHER_INSTRUMENT):
        records = manager.evaluations_for(instrument)
        assert len(records) == 1
        assert records[0].evaluation.instrument == instrument


def test_a_stale_context_for_one_instrument_does_not_affect_another() -> None:
    strategy = b.FakeStrategy("s", reqs=b.requirements(trigger=StrategyTrigger.ON_CONTEXT))
    manager, bus, _ = _wire([strategy])
    _publish(bus, b.make_context(version=5, instrument=b.INSTRUMENT), previous=4)
    _publish(bus, b.make_context(version=1, instrument=b.OTHER_INSTRUMENT))  # fresh for other
    assert len(manager.evaluations_for(b.OTHER_INSTRUMENT)) == 1


# --------------------------------------------------------------------------- #
# Immutability and record shape
# --------------------------------------------------------------------------- #
def test_records_and_evaluations_are_immutable_tuples() -> None:
    strategy = b.FakeStrategy("s", behavior="match")
    manager, bus, _ = _wire([strategy])
    _publish(bus, b.make_context())
    records = manager.evaluations_for(b.INSTRUMENT)
    assert isinstance(records, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        records[0].readiness = Readiness.MISSING_FACTS  # type: ignore[misc]


def test_evaluations_for_unknown_instrument_is_empty() -> None:
    manager, _, _ = _wire([b.FakeStrategy("s")])
    assert manager.evaluations_for(b.OTHER_INSTRUMENT) == ()


# --------------------------------------------------------------------------- #
# Subscription lifecycle and the observation sink
# --------------------------------------------------------------------------- #
def test_subscribe_is_idempotent() -> None:
    strategy = b.FakeStrategy("s", behavior="match")
    manager, bus, _ = _wire([strategy])
    manager.subscribe()  # second call must not double-dispatch
    _publish(bus, b.make_context())
    assert len(strategy.calls) == 1


def test_unsubscribe_stops_evaluation() -> None:
    strategy = b.FakeStrategy("s", behavior="match")
    manager, bus, _ = _wire([strategy])
    manager.unsubscribe()
    _publish(bus, b.make_context())
    assert strategy.calls == []
    assert manager.evaluations_for(b.INSTRUMENT) == ()


def test_sink_receives_each_record() -> None:
    received: list[StrategyEvaluationRecord] = []
    strategies = [b.FakeStrategy("alpha", behavior="match"), b.FakeStrategy("bravo")]
    manager, bus, _ = _wire(strategies, sink=received.append)
    _publish(bus, b.make_context())
    assert [r.strategy_id for r in received] == ["alpha", "bravo"]
