"""Lifecycle-driven requirement orchestration: START/PAUSE/STOP/warmup (P5.4).

Covers ADR-007 D3–D9 and the prompt's §38–§41: requirement registration/retention/
release across the lifecycle, shared requirement union/shrink, historical warmup
through the port (including zero-requirement and failure paths and cross-strategy
isolation), and determinism. Uses test-only fakes for the two capability ports —
no concrete strategies, no Market Engine, no provider.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

import pytest

from app.events.bus import EventBus
from app.market_engine.historical.calendar_window import OutsideCalendarCoverageError
from app.market_engine.historical.requirements import (
    HistoricalRequirement,
    HistoricalRequirementRegistry,
)
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Instrument
from app.strategies.enums import StrategyLifecycleState as State
from app.strategies.registry import StrategyRegistry
from app.strategy_manager.errors import StrategyLifecycleError
from app.strategy_manager.lifecycle import StrategyLifecycle
from app.strategy_manager.live_timeframes import LiveTimeframeRequirementRegistry
from app.strategy_manager.manager import StrategyManager
from app.strategy_manager.requirements_bridge import RequirementsCoordinator
from tests.unit.strategy_manager import builders as b

_M1 = b.ONE_MINUTE
_M15 = Timeframe.minutes(15)
_REFERENCE = b.EVENT_TIME


class FakeSink:
    """Records each effective live-timeframe set pushed to the Market-Engine seam."""

    def __init__(self) -> None:
        self.calls: list[frozenset[Timeframe]] = []

    def set_required_timeframes(self, timeframes: frozenset[Timeframe]) -> None:
        self.calls.append(frozenset(timeframes))

    @property
    def last(self) -> frozenset[Timeframe]:
        return self.calls[-1] if self.calls else frozenset()


class FakeWarmup:
    """A warmup port modelling per-instrument (local) and global warmup outcomes.

    ``withhold`` drops timeframes from every instrument's satisfied set — a *local*
    per-instrument shortfall that, under ADR-007 partial-universe readiness, no longer
    blocks RUNNING. ``raises`` (settable at construction or between starts) makes warmup
    raise a *global* failure that propagates out — modelling
    :class:`OutsideCalendarCoverageError` / an unavailable warmup — and fails START closed.
    """

    def __init__(
        self,
        *,
        withhold: frozenset[Timeframe] = frozenset(),
        raises: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple[tuple[Instrument, ...], tuple[HistoricalRequirement, ...]]] = []
        self._withhold = withhold
        self.raises = raises

    async def warmup(
        self,
        instruments: Sequence[Instrument],
        effective_requirements: Sequence[HistoricalRequirement],
        *,
        reference: datetime,
    ) -> Mapping[Instrument, frozenset[Timeframe]]:
        self.calls.append((tuple(instruments), tuple(effective_requirements)))
        if self.raises is not None:
            raise self.raises
        satisfied = frozenset(req.timeframe for req in effective_requirements) - self._withhold
        return {instrument: satisfied for instrument in instruments}


def _coordinator(
    *,
    instruments: Sequence[Instrument] = (b.INSTRUMENT,),
    historical: HistoricalRequirementRegistry | None = None,
    live: LiveTimeframeRequirementRegistry | None = None,
    sink: FakeSink | None = None,
    warmup: FakeWarmup | None = None,
) -> tuple[
    RequirementsCoordinator, HistoricalRequirementRegistry, LiveTimeframeRequirementRegistry
]:
    historical = historical if historical is not None else HistoricalRequirementRegistry()
    live = live if live is not None else LiveTimeframeRequirementRegistry()
    coordinator = RequirementsCoordinator(
        instruments=instruments,
        historical=historical,
        live=live,
        sink=sink if sink is not None else FakeSink(),
        warmup=warmup if warmup is not None else FakeWarmup(),
    )
    return coordinator, historical, live


def _manager(
    strategies: list[b.FakeStrategy],
    *,
    coordinator: RequirementsCoordinator,
    configs: dict | None = None,
    error_threshold: int = 3,
) -> tuple[StrategyManager, StrategyLifecycle]:
    registry = StrategyRegistry()
    lifecycle = StrategyLifecycle()
    for strategy in strategies:
        registry.register(strategy)
        lifecycle.register(strategy.descriptor.strategy_id)  # REGISTERED, ready to start
    ids = [s.descriptor.strategy_id for s in strategies]
    manager = StrategyManager(
        registry=registry,
        lifecycle=lifecycle,
        configurations=configs if configs is not None else b.default_configs(*ids),
        error_threshold=error_threshold,
        bus=EventBus(),
        requirements=coordinator,
    )
    return manager, lifecycle


def _historical_reqs(*specs: tuple[Timeframe, int]) -> tuple[HistoricalRequirement, ...]:
    return tuple(HistoricalRequirement(timeframe=tf, lookback=lb) for tf, lb in specs)


# --------------------------------------------------------------------------- #
# START — registration + readiness before RUNNING (§18, §20, §38)
# --------------------------------------------------------------------------- #
async def test_start_registers_requirements_and_reaches_running() -> None:
    coordinator, historical, live = _coordinator()
    strategy = b.FakeStrategy(
        "s", reqs=b.requirements(live_timeframes=(_M1,), historical=(b.historical_requirement(2),))
    )
    manager, lifecycle = _manager([strategy], coordinator=coordinator)

    state = await manager.start("s", reference=_REFERENCE)

    assert state is State.RUNNING
    assert lifecycle.state_of("s") is State.RUNNING
    assert live.effective_timeframes() == frozenset({_M1})
    assert {req.timeframe for req in historical.effective_requirements()} == {_M1}


async def test_start_applies_the_effective_live_union_to_the_sink() -> None:
    sink = FakeSink()
    coordinator, _, _ = _coordinator(sink=sink)
    strategy = b.FakeStrategy("s", reqs=b.requirements(live_timeframes=(_M1, _M15)))
    manager, _ = _manager([strategy], coordinator=coordinator)

    await manager.start("s", reference=_REFERENCE)
    assert sink.last == frozenset({_M1, _M15})


async def test_partial_warmup_still_reaches_running_and_retains_requirements() -> None:
    # Local (per-instrument) shortfall: warmup executes but leaves the required timeframe
    # unsatisfied. Under ADR-007 partial-universe readiness (PUR2/PUR3) the strategy still
    # reaches RUNNING — unsatisfied instruments are skipped per-context at evaluation time.
    warmup = FakeWarmup(withhold=frozenset({_M1}))  # required historical tf left unsatisfied
    coordinator, historical, _ = _coordinator(warmup=warmup)
    strategy = b.FakeStrategy("s", reqs=b.requirements(historical=(b.historical_requirement(2),)))
    manager, lifecycle = _manager([strategy], coordinator=coordinator)

    state = await manager.start("s", reference=_REFERENCE)

    assert state is State.RUNNING
    assert lifecycle.state_of("s") is State.RUNNING
    assert warmup.calls  # the warmup mechanism actually executed
    assert historical.effective_requirements()  # requirement registered


async def test_global_warmup_failure_prevents_running_and_retains_requirements() -> None:
    # Global failure: warmup raises (OutsideCalendarCoverageError). START fails closed.
    warmup = FakeWarmup(raises=OutsideCalendarCoverageError("outside authoritative coverage"))
    coordinator, historical, live = _coordinator(warmup=warmup)
    strategy = b.FakeStrategy("s", reqs=b.requirements(historical=(b.historical_requirement(2),)))
    manager, lifecycle = _manager([strategy], coordinator=coordinator)

    state = await manager.start("s", reference=_REFERENCE)

    assert state is State.ERROR
    assert lifecycle.state_of("s") is State.ERROR
    # ERROR retains acquired requirements (ADR-007 D7): a STARTING failure never rolls back.
    assert historical.effective_requirements()  # still registered
    assert live.requirements_for("s") == frozenset()  # (strategy declared no live tf)


async def test_missing_configuration_fails_before_any_requirement_is_registered() -> None:
    coordinator, historical, live = _coordinator()
    strategy = b.FakeStrategy("s", reqs=b.requirements(live_timeframes=(_M1,)))
    manager, lifecycle = _manager([strategy], coordinator=coordinator, configs={})

    state = await manager.start("s", reference=_REFERENCE)

    assert state is State.ERROR
    # Config validation precedes registration (ADR-007 D3 order): nothing acquired.
    assert historical.effective_requirements() == ()
    assert live.effective_timeframes() == frozenset()


async def test_lifecycle_commands_require_a_coordinator() -> None:
    strategy = b.FakeStrategy("s")
    registry = StrategyRegistry()
    registry.register(strategy)
    lifecycle = StrategyLifecycle()
    lifecycle.register("s")
    manager = StrategyManager(
        registry=registry,
        lifecycle=lifecycle,
        configurations=b.default_configs("s"),
        error_threshold=3,
        bus=EventBus(),
    )
    with pytest.raises(StrategyLifecycleError):
        await manager.start("s", reference=_REFERENCE)


# --------------------------------------------------------------------------- #
# Zero historical requirements (§17, §40)
# --------------------------------------------------------------------------- #
async def test_live_only_strategy_triggers_no_historical_fetch() -> None:
    warmup = FakeWarmup()
    coordinator, _, _ = _coordinator(warmup=warmup)
    strategy = b.FakeStrategy("s", reqs=b.requirements(live_timeframes=(_M1,)))  # no historical
    manager, lifecycle = _manager([strategy], coordinator=coordinator)

    state = await manager.start("s", reference=_REFERENCE)

    assert state is State.RUNNING
    assert warmup.calls == []  # zero historical requirements → no warmup call


# --------------------------------------------------------------------------- #
# Warmup invocation + effective union (§40)
# --------------------------------------------------------------------------- #
async def test_start_warms_through_the_port_with_the_effective_union() -> None:
    warmup = FakeWarmup()
    historical_reg = HistoricalRequirementRegistry()
    coordinator, historical, _ = _coordinator(historical=historical_reg, warmup=warmup)
    a = b.FakeStrategy("a", reqs=b.requirements(historical=_historical_reqs((_M1, 100))))
    bb = b.FakeStrategy(
        "b", reqs=b.requirements(historical=_historical_reqs((_M1, 20), (_M15, 50)))
    )
    manager, _ = _manager([a, bb], coordinator=coordinator)

    await manager.start("a", reference=_REFERENCE)
    await manager.start("b", reference=_REFERENCE)

    # The most recent warmup call carried the max-lookback union across both strategies.
    _instruments, effective = warmup.calls[-1]
    assert set(effective) == {
        HistoricalRequirement(timeframe=_M1, lookback=100),
        HistoricalRequirement(timeframe=_M15, lookback=50),
    }


async def test_one_strategy_global_failure_does_not_disturb_another_running_strategy() -> None:
    # A global warmup failure at one strategy's START isolates to that strategy; a strategy
    # already RUNNING (warmed earlier) is untouched, and its requirements survive.
    warmup = FakeWarmup()
    coordinator, historical, _ = _coordinator(warmup=warmup)
    good = b.FakeStrategy("b_ok", reqs=b.requirements(historical=_historical_reqs((_M1, 10))))
    bad = b.FakeStrategy("a_bad", reqs=b.requirements(historical=_historical_reqs((_M15, 10))))
    manager, lifecycle = _manager([good, bad], coordinator=coordinator)

    assert await manager.start("b_ok", reference=_REFERENCE) is State.RUNNING
    warmup.raises = OutsideCalendarCoverageError("outside coverage")  # a_bad hits a global fault
    assert await manager.start("a_bad", reference=_REFERENCE) is State.ERROR

    assert lifecycle.state_of("b_ok") is State.RUNNING  # untouched
    assert HistoricalRequirement(timeframe=_M1, lookback=10) in historical.effective_requirements()


# --------------------------------------------------------------------------- #
# PAUSE / RESUME retain requirements (§21, §22, §38)
# --------------------------------------------------------------------------- #
async def test_pause_retains_requirements_and_does_not_reapply_the_union() -> None:
    sink = FakeSink()
    warmup = FakeWarmup()
    coordinator, historical, live = _coordinator(sink=sink, warmup=warmup)
    strategy = b.FakeStrategy(
        "s", reqs=b.requirements(live_timeframes=(_M1,), historical=(b.historical_requirement(2),))
    )
    manager, lifecycle = _manager([strategy], coordinator=coordinator)
    await manager.start("s", reference=_REFERENCE)
    applications_after_start = len(sink.calls)

    manager.pause("s")

    assert lifecycle.state_of("s") is State.PAUSED
    assert live.effective_timeframes() == frozenset({_M1})  # retained
    assert historical.effective_requirements()  # retained
    assert len(sink.calls) == applications_after_start  # union not re-applied


async def test_resume_does_not_re_register_or_re_warm() -> None:
    warmup = FakeWarmup()
    coordinator, _, live = _coordinator(warmup=warmup)
    strategy = b.FakeStrategy(
        "s", reqs=b.requirements(live_timeframes=(_M1,), historical=(b.historical_requirement(2),))
    )
    manager, lifecycle = _manager([strategy], coordinator=coordinator)
    await manager.start("s", reference=_REFERENCE)
    warmups_after_start = len(warmup.calls)

    manager.pause("s")
    manager.resume("s")

    assert lifecycle.state_of("s") is State.RUNNING
    assert len(warmup.calls) == warmups_after_start  # no re-warm on resume
    assert live.requirements_for("s") == frozenset({_M1})  # unchanged


# --------------------------------------------------------------------------- #
# RUNNING -> ERROR retains requirements (§23)
# --------------------------------------------------------------------------- #
async def test_running_to_error_retains_shared_requirements() -> None:
    sink = FakeSink()
    coordinator, historical, live = _coordinator(sink=sink)
    a = b.FakeStrategy("a", reqs=b.requirements(live_timeframes=(_M1,)))
    bb = b.FakeStrategy("b", reqs=b.requirements(live_timeframes=(_M1, _M15)))
    manager, lifecycle = _manager([a, bb], coordinator=coordinator)
    await manager.start("a", reference=_REFERENCE)
    await manager.start("b", reference=_REFERENCE)
    applications = len(sink.calls)

    # RUNNING -> ERROR is what P5.3 escalation performs; it must not release requirements.
    lifecycle.mark_error("a")

    assert lifecycle.state_of("a") is State.ERROR
    assert live.effective_timeframes() == frozenset({_M1, _M15})  # 5m/15m still active
    assert live.requirements_for("a") == frozenset({_M1})  # A's requirement retained
    assert len(sink.calls) == applications  # no release, no re-application


# --------------------------------------------------------------------------- #
# STOP / FORCE STOP release requirements (§24, §25)
# --------------------------------------------------------------------------- #
async def test_stop_releases_requirements_and_shrinks_the_union() -> None:
    sink = FakeSink()
    coordinator, historical, live = _coordinator(sink=sink)
    strategy = b.FakeStrategy(
        "s", reqs=b.requirements(live_timeframes=(_M1,), historical=(b.historical_requirement(2),))
    )
    manager, lifecycle = _manager([strategy], coordinator=coordinator)
    await manager.start("s", reference=_REFERENCE)

    manager.stop("s")

    assert lifecycle.state_of("s") is State.STOPPED
    assert live.effective_timeframes() == frozenset()
    assert historical.effective_requirements() == ()
    assert sink.last == frozenset()  # empty union applied to the seam


async def test_force_stop_from_running_releases_requirements() -> None:
    coordinator, historical, live = _coordinator()
    strategy = b.FakeStrategy("s", reqs=b.requirements(live_timeframes=(_M1,)))
    manager, lifecycle = _manager([strategy], coordinator=coordinator)
    await manager.start("s", reference=_REFERENCE)

    manager.force_stop("s")

    assert lifecycle.state_of("s") is State.STOPPED
    assert live.effective_timeframes() == frozenset()


async def test_force_stop_from_error_releases_requirements() -> None:
    warmup = FakeWarmup(raises=OutsideCalendarCoverageError("outside coverage"))
    coordinator, historical, live = _coordinator(warmup=warmup)
    strategy = b.FakeStrategy("s", reqs=b.requirements(historical=(b.historical_requirement(2),)))
    manager, lifecycle = _manager([strategy], coordinator=coordinator)
    await manager.start("s", reference=_REFERENCE)  # -> ERROR (global failure, retained)
    assert lifecycle.state_of("s") is State.ERROR

    manager.force_stop("s")

    assert lifecycle.state_of("s") is State.STOPPED
    assert historical.effective_requirements() == ()  # released on force stop


# --------------------------------------------------------------------------- #
# Shared requirements union + shrink (§28, §39) — the ADR normative example
# --------------------------------------------------------------------------- #
def test_shared_requirement_union_and_shrink_matches_the_adr_example() -> None:
    coordinator, historical, live = _coordinator()
    a = b.requirements(historical=_historical_reqs((_M1, 100)), live_timeframes=(_M1,))
    bb = b.requirements(
        historical=_historical_reqs((_M1, 20), (_M15, 50)), live_timeframes=(_M1, _M15)
    )
    coordinator.register("a", a)
    coordinator.register("b", bb)

    assert set(historical.effective_requirements()) == {
        HistoricalRequirement(timeframe=_M1, lookback=100),
        HistoricalRequirement(timeframe=_M15, lookback=50),
    }
    assert live.effective_timeframes() == frozenset({_M1, _M15})

    coordinator.release("a")
    assert set(historical.effective_requirements()) == {
        HistoricalRequirement(timeframe=_M1, lookback=20),  # shrank from 100 -> 20
        HistoricalRequirement(timeframe=_M15, lookback=50),
    }
    assert live.effective_timeframes() == frozenset({_M1, _M15})  # b still needs both

    coordinator.release("b")
    assert historical.effective_requirements() == ()
    assert live.effective_timeframes() == frozenset()


# --------------------------------------------------------------------------- #
# STOPPED -> START re-registers; ERROR -> START is idempotent (§26, §27)
# --------------------------------------------------------------------------- #
async def test_stopped_then_start_re_registers_requirements() -> None:
    coordinator, historical, live = _coordinator()
    strategy = b.FakeStrategy("s", reqs=b.requirements(live_timeframes=(_M1,)))
    manager, lifecycle = _manager([strategy], coordinator=coordinator)
    await manager.start("s", reference=_REFERENCE)
    manager.stop("s")
    assert live.effective_timeframes() == frozenset()

    state = await manager.start("s", reference=_REFERENCE)

    assert state is State.RUNNING
    assert live.effective_timeframes() == frozenset({_M1})  # re-registered


async def test_error_then_start_is_idempotent_and_creates_no_duplicate_consumer() -> None:
    warmup = FakeWarmup(raises=OutsideCalendarCoverageError("outside coverage"))
    coordinator, historical, live = _coordinator(warmup=warmup)
    strategy = b.FakeStrategy(
        "s", reqs=b.requirements(historical=_historical_reqs((_M1, 30)), live_timeframes=(_M1,))
    )
    manager, lifecycle = _manager([strategy], coordinator=coordinator)
    await manager.start("s", reference=_REFERENCE)  # -> ERROR, requirements retained
    assert lifecycle.state_of("s") is State.ERROR

    # Requirements already retained; restarting must not double-register.
    state = await manager.start("s", reference=_REFERENCE)

    assert state is State.ERROR  # still globally failing
    assert live.snapshot() == (("s", (_M1,)),)  # exactly one consumer entry
    assert historical.effective_requirements() == (
        HistoricalRequirement(timeframe=_M1, lookback=30),
    )  # not doubled


# --------------------------------------------------------------------------- #
# Determinism (§30, §41)
# --------------------------------------------------------------------------- #
def test_registration_order_does_not_affect_effective_requirements() -> None:
    a = b.requirements(historical=_historical_reqs((_M1, 100)), live_timeframes=(_M1,))
    bb = b.requirements(
        historical=_historical_reqs((_M1, 20), (_M15, 50)), live_timeframes=(_M1, _M15)
    )
    forward, forward_hist, forward_live = _coordinator()
    forward.register("a", a)
    forward.register("b", bb)
    reverse, reverse_hist, reverse_live = _coordinator()
    reverse.register("b", bb)
    reverse.register("a", a)

    assert forward_hist.effective_requirements() == reverse_hist.effective_requirements()
    assert forward_live.effective_timeframes() == reverse_live.effective_timeframes()
    assert forward_live.snapshot() == reverse_live.snapshot()


async def test_same_command_sequence_yields_the_same_snapshots() -> None:
    def build() -> tuple[
        StrategyManager, LiveTimeframeRequirementRegistry, HistoricalRequirementRegistry
    ]:
        coordinator, historical, live = _coordinator()
        strategies = [
            b.FakeStrategy("a", reqs=b.requirements(live_timeframes=(_M1,))),
            b.FakeStrategy("b", reqs=b.requirements(live_timeframes=(_M1, _M15))),
        ]
        manager, _ = _manager(strategies, coordinator=coordinator)
        return manager, live, historical

    first_manager, first_live, first_hist = build()
    await first_manager.start("a", reference=_REFERENCE)
    await first_manager.start("b", reference=_REFERENCE)

    second_manager, second_live, second_hist = build()
    await second_manager.start("a", reference=_REFERENCE)
    await second_manager.start("b", reference=_REFERENCE)

    assert first_live.snapshot() == second_live.snapshot()
    assert first_hist.effective_requirements() == second_hist.effective_requirements()


# --------------------------------------------------------------------------- #
# ADR-007 partial-universe readiness (PUR2/PUR3): START is infrastructure-level.
# A strategy reaches RUNNING once the warmup mechanism executes without a GLOBAL
# failure — regardless of how many instruments are individually satisfied.
# --------------------------------------------------------------------------- #
def _many_instruments(count: int) -> tuple[Instrument, ...]:
    return tuple(Instrument(exchange="NSE", symbol=f"SYM{index:04d}") for index in range(count))


async def test_zero_satisfied_universe_still_reaches_running() -> None:
    # No instrument is satisfied, yet START succeeds: warmup executed without a global
    # failure. Every instrument is simply skipped at evaluation time (no fabrication).
    warmup = FakeWarmup(withhold=frozenset({_M1}))  # nothing satisfied across the universe
    coordinator, _, _ = _coordinator(instruments=(b.INSTRUMENT, b.OTHER_INSTRUMENT), warmup=warmup)
    strategy = b.FakeStrategy("s", reqs=b.requirements(historical=(b.historical_requirement(2),)))
    manager, lifecycle = _manager([strategy], coordinator=coordinator)

    assert await manager.start("s", reference=_REFERENCE) is State.RUNNING
    assert lifecycle.state_of("s") is State.RUNNING


async def test_large_universe_partial_warmup_reaches_running() -> None:
    # Scale (~200 instruments): START reaches RUNNING as soon as warmup executes over the
    # whole universe, independent of per-instrument satisfaction. The honest PARTIAL count
    # is a scanner concern (evaluated_count < expected_count), proven in the scanner/E2E.
    universe = _many_instruments(208)
    warmup = FakeWarmup()
    coordinator, _, _ = _coordinator(instruments=universe, warmup=warmup)
    strategy = b.FakeStrategy("s", reqs=b.requirements(historical=(b.historical_requirement(2),)))
    manager, lifecycle = _manager([strategy], coordinator=coordinator)

    assert await manager.start("s", reference=_REFERENCE) is State.RUNNING
    assert lifecycle.state_of("s") is State.RUNNING
    warmed_instruments, _effective = warmup.calls[-1]
    assert len(warmed_instruments) == 208  # the whole universe was offered to warmup


async def test_multi_strategy_partial_universe_isolation() -> None:
    # Two strategies over the same universe both reach RUNNING under a partial warmup; the
    # shared effective union is unaffected by either strategy's per-instrument shortfall.
    warmup = FakeWarmup(withhold=frozenset({_M15}))
    historical_reg = HistoricalRequirementRegistry()
    coordinator, historical, _ = _coordinator(
        instruments=(b.INSTRUMENT, b.OTHER_INSTRUMENT), historical=historical_reg, warmup=warmup
    )
    one = b.FakeStrategy("one", reqs=b.requirements(historical=_historical_reqs((_M1, 10))))
    two = b.FakeStrategy("two", reqs=b.requirements(historical=_historical_reqs((_M15, 20))))
    manager, lifecycle = _manager([one, two], coordinator=coordinator)

    assert await manager.start("one", reference=_REFERENCE) is State.RUNNING
    assert await manager.start("two", reference=_REFERENCE) is State.RUNNING
    assert lifecycle.state_of("one") is State.RUNNING
    assert lifecycle.state_of("two") is State.RUNNING
    assert set(historical.effective_requirements()) == {
        HistoricalRequirement(timeframe=_M1, lookback=10),
        HistoricalRequirement(timeframe=_M15, lookback=20),
    }
