"""Manager-level result publication, dedup lifecycle, and determinism (P5.5).

Drives the StrategyManager through the in-process EventBus and asserts the
StrategyResultsPublished contract: promote → dedup → rank → publish, suppression
when nothing is material, dedup retention across PAUSE/ERROR, dedup reset on
STOP/FORCE STOP, per-instrument isolation, replay determinism, and that the
immutable StrategyResult never carries a rank. Uses only test fakes — no concrete
strategy, no Market Engine, no provider.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from app.events.bus import EventBus
from app.market_engine.context import MarketContext
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.historical.requirements import (
    HistoricalRequirement,
    HistoricalRequirementRegistry,
)
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument
from app.strategies.enums import EvaluationStatus, StrategyLifecycleState
from app.strategies.registry import StrategyRegistry
from app.strategies.results import StrategyEvaluation
from app.strategy_manager.events import StrategyResultsPublished
from app.strategy_manager.lifecycle import StrategyLifecycle
from app.strategy_manager.live_timeframes import LiveTimeframeRequirementRegistry
from app.strategy_manager.manager import StrategyManager
from app.strategy_manager.requirements_bridge import RequirementsCoordinator
from tests.unit.strategy_manager import builders as b

_REFERENCE = b.EVENT_TIME


class _FakeSink:
    def set_required_timeframes(self, timeframes: frozenset[Timeframe]) -> None:
        pass


class _FakeWarmup:
    async def warmup(
        self,
        instruments: Sequence[Instrument],
        effective_requirements: Sequence[HistoricalRequirement],
        *,
        reference: datetime,
    ) -> Mapping[Instrument, frozenset[Timeframe]]:
        return {instrument: frozenset() for instrument in instruments}


class _Recorder:
    """Captures every StrategyResultsPublished event delivered on the bus."""

    def __init__(self) -> None:
        self.events: list[StrategyResultsPublished] = []

    def __call__(self, event: StrategyResultsPublished) -> None:
        self.events.append(event)


def matcher(score: str) -> Callable[[MarketContext], StrategyEvaluation]:
    """An evaluator that always matches at a fixed score."""
    return lambda context: b.evaluation(context, status=EvaluationStatus.MATCHED, score=score)


def scripted(*outcomes: str) -> Callable[[MarketContext], StrategyEvaluation]:
    """An evaluator consuming one outcome per call: a score string, or ``"raise"``."""
    remaining = iter(outcomes)

    def _evaluate(context: MarketContext) -> StrategyEvaluation:
        outcome = next(remaining)
        if outcome == "raise":
            raise RuntimeError("scripted failure")
        return b.evaluation(context, status=EvaluationStatus.MATCHED, score=outcome)

    return _evaluate


def _coordinator(instruments: Sequence[Instrument] = (b.INSTRUMENT,)) -> RequirementsCoordinator:
    return RequirementsCoordinator(
        instruments=instruments,
        historical=HistoricalRequirementRegistry(),
        live=LiveTimeframeRequirementRegistry(),
        sink=_FakeSink(),
        warmup=_FakeWarmup(),
    )


def _manager(
    *strategies: b.FakeStrategy,
    bus: EventBus,
    recorder: _Recorder,
    running: bool,
    coordinator: RequirementsCoordinator | None = None,
    error_threshold: int = 3,
) -> tuple[StrategyManager, StrategyLifecycle]:
    registry = StrategyRegistry()
    lifecycle = StrategyLifecycle()
    for strategy in strategies:
        registry.register(strategy)
        lifecycle.register(strategy.descriptor.strategy_id)
        if running:
            lifecycle.start(strategy.descriptor.strategy_id)
            lifecycle.mark_running(strategy.descriptor.strategy_id)
    configs = b.default_configs(*(s.descriptor.strategy_id for s in strategies))
    manager = StrategyManager(
        registry=registry,
        lifecycle=lifecycle,
        configurations=configs,
        error_threshold=error_threshold,
        bus=bus,
        requirements=coordinator,
    )
    manager.subscribe()
    bus.subscribe(StrategyResultsPublished, recorder)
    return manager, lifecycle


def _drive(bus: EventBus, *, version: int, instrument: Instrument = b.INSTRUMENT) -> None:
    context = b.make_context(
        version=version, instrument=instrument, session=b.make_session(b.MarketState.LIVE_SESSION)
    )
    if version == 1:
        bus.publish(MarketContextCreated(context=context))
    else:
        bus.publish(MarketContextUpdated(context=context, previous_version=version - 1))


# --------------------------------------------------------------------------- #
# Publication contract
# --------------------------------------------------------------------------- #
def test_a_material_cycle_publishes_ranked_results() -> None:
    bus, recorder = EventBus(), _Recorder()
    _manager(
        b.FakeStrategy("alpha", evaluator=matcher("5")),
        b.FakeStrategy("beta", evaluator=matcher("9")),
        bus=bus,
        recorder=recorder,
        running=True,
    )
    _drive(bus, version=1)
    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.instrument == b.INSTRUMENT
    assert event.context_version == 1
    assert len(event.results) == 2
    assert [item.result.strategy_id for item in event.ranked] == ["beta", "alpha"]
    assert [item.rank for item in event.ranked] == [1, 2]


def test_publication_carries_the_session_trading_date() -> None:
    # ADR-012 NCRS9 / §42: the event's trading_date is the context session's date verbatim
    # (never a wall-clock/derived value); the cross-instrument scanner keys snapshots on it.
    bus, recorder = EventBus(), _Recorder()
    _manager(
        b.FakeStrategy("alpha", evaluator=matcher("5")), bus=bus, recorder=recorder, running=True
    )
    _drive(bus, version=1)
    expected = b.make_session(b.MarketState.LIVE_SESSION).trading_date
    assert recorder.events[0].trading_date == expected


def test_published_result_carries_no_rank() -> None:
    bus, recorder = EventBus(), _Recorder()
    _manager(
        b.FakeStrategy("alpha", evaluator=matcher("5")), bus=bus, recorder=recorder, running=True
    )
    _drive(bus, version=1)
    result = recorder.events[0].results[0]
    assert not hasattr(result, "rank")  # rank lives only on the RankedStrategyResult projection


def test_no_publication_when_every_result_is_suppressed() -> None:
    bus, recorder = EventBus(), _Recorder()
    _manager(
        b.FakeStrategy("alpha", evaluator=matcher("5")), bus=bus, recorder=recorder, running=True
    )
    _drive(bus, version=1)  # first observation — material
    _drive(bus, version=2)  # identical content, only the version moved — suppressed
    assert len(recorder.events) == 1


def test_publication_is_isolated_per_instrument() -> None:
    bus, recorder = EventBus(), _Recorder()
    _manager(
        b.FakeStrategy("alpha", evaluator=matcher("5")), bus=bus, recorder=recorder, running=True
    )
    _drive(bus, version=1, instrument=b.INSTRUMENT)
    _drive(bus, version=1, instrument=b.OTHER_INSTRUMENT)
    assert [event.instrument for event in recorder.events] == [b.INSTRUMENT, b.OTHER_INSTRUMENT]


def test_publication_is_strategy_agnostic() -> None:
    # An arbitrary, non-concrete id flows through unchanged — the manager knows the
    # contract, never a specific strategy (docs/07 §6, rule 29).
    bus, recorder = EventBus(), _Recorder()
    _manager(
        b.FakeStrategy("some_unknown_plugin_42", evaluator=matcher("5")),
        bus=bus,
        recorder=recorder,
        running=True,
    )
    _drive(bus, version=1)
    assert recorder.events[0].results[0].strategy_id == "some_unknown_plugin_42"


def test_replay_of_the_same_sequence_is_deterministic() -> None:
    def run() -> list[StrategyResultsPublished]:
        bus, recorder = EventBus(), _Recorder()
        _manager(
            b.FakeStrategy("alpha", evaluator=scripted("5", "7")),
            bus=bus,
            recorder=recorder,
            running=True,
        )
        _drive(bus, version=1)
        _drive(bus, version=2)
        return recorder.events

    assert run() == run()


# --------------------------------------------------------------------------- #
# Emission-state lifecycle: PAUSE/ERROR retain; STOP/FORCE STOP reset
# --------------------------------------------------------------------------- #
async def test_pause_resume_retains_emission_state() -> None:
    bus, recorder = EventBus(), _Recorder()
    manager, _ = _manager(
        b.FakeStrategy("alpha", evaluator=matcher("5")),
        bus=bus,
        recorder=recorder,
        running=False,
        coordinator=_coordinator(),
    )
    await manager.start("alpha", reference=_REFERENCE)
    _drive(bus, version=1)  # material
    manager.pause("alpha")
    manager.resume("alpha")
    _drive(bus, version=2)  # identical — retained state suppresses it
    assert len(recorder.events) == 1


async def test_stop_then_restart_resets_emission_state() -> None:
    bus, recorder = EventBus(), _Recorder()
    manager, _ = _manager(
        b.FakeStrategy("alpha", evaluator=matcher("5")),
        bus=bus,
        recorder=recorder,
        running=False,
        coordinator=_coordinator(),
    )
    await manager.start("alpha", reference=_REFERENCE)
    _drive(bus, version=1)  # material — published
    manager.stop("alpha")  # tears down the session footprint → dedup reset
    await manager.start("alpha", reference=_REFERENCE)
    _drive(bus, version=2)  # identical content re-emits after the reset
    assert len(recorder.events) == 2


async def test_force_stop_then_restart_resets_emission_state() -> None:
    bus, recorder = EventBus(), _Recorder()
    manager, _ = _manager(
        b.FakeStrategy("alpha", evaluator=matcher("5")),
        bus=bus,
        recorder=recorder,
        running=False,
        coordinator=_coordinator(),
    )
    await manager.start("alpha", reference=_REFERENCE)
    _drive(bus, version=1)
    manager.force_stop("alpha")
    await manager.start("alpha", reference=_REFERENCE)
    _drive(bus, version=2)
    assert len(recorder.events) == 2


async def test_error_then_restart_retains_emission_state() -> None:
    bus, recorder = EventBus(), _Recorder()
    manager, lifecycle = _manager(
        b.FakeStrategy("alpha", evaluator=scripted("5", "raise", "5")),
        bus=bus,
        recorder=recorder,
        running=False,
        coordinator=_coordinator(),
        error_threshold=1,
    )
    await manager.start("alpha", reference=_REFERENCE)
    _drive(bus, version=1)  # match@5 — published; dedup records it
    _drive(bus, version=2)  # raises → threshold(1) → ERROR; requirements + dedup retained
    assert lifecycle.state_of("alpha") is StrategyLifecycleState.ERROR
    await manager.start("alpha", reference=_REFERENCE)  # ERROR → STARTING → RUNNING
    _drive(bus, version=3)  # identical match@5 — suppressed because state was retained
    assert len(recorder.events) == 1
