"""Strategy lifecycle FSM: transitions, fail-closed, isolation, determinism (P5.2)."""

from __future__ import annotations

import pytest

from app.strategies.enums import StrategyLifecycleState as State
from app.strategy_manager import (
    InvalidStrategyTransitionError,
    StrategyAlreadyTrackedError,
    StrategyLifecycle,
    StrategyNotTrackedError,
    StrategyRuntimeStatus,
)


def _running(strategy_id: str = "s") -> StrategyLifecycle:
    fsm = StrategyLifecycle()
    fsm.register(strategy_id)
    fsm.start(strategy_id)
    fsm.mark_running(strategy_id)
    return fsm


# --------------------------------------------------------------------------- #
# Registration & initial state
# --------------------------------------------------------------------------- #
def test_initial_state_is_registered() -> None:
    fsm = StrategyLifecycle()
    assert fsm.register("s") is State.REGISTERED
    assert fsm.state_of("s") is State.REGISTERED


def test_duplicate_registration_fails_closed_without_reset() -> None:
    fsm = _running()
    with pytest.raises(StrategyAlreadyTrackedError):
        fsm.register("s")
    assert fsm.state_of("s") is State.RUNNING  # not reset to REGISTERED


def test_commands_on_untracked_strategy_fail_closed() -> None:
    fsm = StrategyLifecycle()
    for command in (fsm.start, fsm.pause, fsm.stop, fsm.mark_error, fsm.shutdown, fsm.state_of):
        with pytest.raises(StrategyNotTrackedError):
            command("ghost")


# --------------------------------------------------------------------------- #
# Legal transitions (corrected ADR-007 D2)
# --------------------------------------------------------------------------- #
def test_full_normal_lifecycle_path() -> None:
    fsm = StrategyLifecycle()
    fsm.register("s")
    assert fsm.start("s") is State.STARTING
    assert fsm.mark_running("s") is State.RUNNING
    assert fsm.pause("s") is State.PAUSED
    assert fsm.resume("s") is State.RUNNING
    assert fsm.stop("s") is State.STOPPED
    assert fsm.start("s") is State.STARTING  # STOPPED -> STARTING (corrected D2)


def test_starting_can_fault_and_restart() -> None:
    fsm = StrategyLifecycle()
    fsm.register("s")
    fsm.start("s")
    assert fsm.mark_error("s") is State.ERROR  # STARTING -> ERROR
    assert fsm.start("s") is State.STARTING  # ERROR -> STARTING
    fsm.mark_running("s")
    assert fsm.mark_error("s") is State.ERROR  # RUNNING -> ERROR
    assert fsm.stop("s") is State.STOPPED  # ERROR -> STOPPED


def test_paused_can_stop() -> None:
    fsm = _running()
    fsm.pause("s")
    assert fsm.stop("s") is State.STOPPED


# --------------------------------------------------------------------------- #
# Illegal transitions fail closed and are atomic
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("setup", "command", "expected_state"),
    [
        ("registered", "mark_running", State.REGISTERED),
        ("registered", "pause", State.REGISTERED),
        ("registered", "resume", State.REGISTERED),
        ("registered", "stop", State.REGISTERED),
        ("starting", "pause", State.STARTING),
        ("starting", "resume", State.STARTING),
        ("paused", "start", State.PAUSED),
        ("paused", "mark_error", State.PAUSED),
        ("stopped", "pause", State.STOPPED),
        ("stopped", "mark_error", State.STOPPED),
        ("stopped", "resume", State.STOPPED),
        ("error", "pause", State.ERROR),
        ("error", "resume", State.ERROR),
        ("error", "mark_running", State.ERROR),
    ],
)
def test_illegal_transitions_reject_and_preserve_state(
    setup: str, command: str, expected_state: State
) -> None:
    fsm = StrategyLifecycle()
    fsm.register("s")
    if setup in {"starting", "running", "paused", "stopped", "error"}:
        fsm.start("s")
    if setup in {"running", "paused", "stopped"}:
        fsm.mark_running("s")
    if setup == "paused":
        fsm.pause("s")
    if setup == "stopped":
        fsm.stop("s")
    if setup == "error":
        fsm.mark_error("s")
    assert fsm.state_of("s") is expected_state
    with pytest.raises(InvalidStrategyTransitionError):
        getattr(fsm, command)("s")
    assert fsm.state_of("s") is expected_state  # unchanged (atomic)


def test_stopped_never_jumps_directly_to_running() -> None:
    fsm = _running()
    fsm.stop("s")
    with pytest.raises(InvalidStrategyTransitionError):
        fsm.mark_running("s")  # only valid from STARTING


# --------------------------------------------------------------------------- #
# Force stop
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("setup", ["running", "paused", "error"])
def test_force_stop_from_authorized_states(setup: str) -> None:
    fsm = StrategyLifecycle()
    fsm.register("s")
    fsm.start("s")
    if setup != "error":
        fsm.mark_running("s")
    if setup == "paused":
        fsm.pause("s")
    if setup == "error":
        fsm.mark_error("s")
    assert fsm.force_stop("s") is State.STOPPED


def test_force_stop_from_starting_requires_clean_cancellable_proof() -> None:
    fsm = StrategyLifecycle()
    fsm.register("s")
    fsm.start("s")
    with pytest.raises(InvalidStrategyTransitionError):
        fsm.force_stop("s")  # default fail-closed
    assert fsm.state_of("s") is State.STARTING
    assert fsm.force_stop("s", clean_cancellable=True) is State.STOPPED


def test_force_stop_from_registered_or_stopped_rejected() -> None:
    fsm = StrategyLifecycle()
    fsm.register("s")
    with pytest.raises(InvalidStrategyTransitionError):
        fsm.force_stop("s")  # REGISTERED not authorized
    fsm.start("s")
    fsm.mark_running("s")
    fsm.stop("s")
    with pytest.raises(InvalidStrategyTransitionError):
        fsm.force_stop("s")  # STOPPED not a silent idempotent success


# --------------------------------------------------------------------------- #
# Shutdown
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "setup", ["registered", "starting", "running", "paused", "stopped", "error"]
)
def test_shutdown_from_any_non_shutdown_state(setup: str) -> None:
    fsm = StrategyLifecycle()
    fsm.register("s")
    if setup != "registered":
        fsm.start("s")
    if setup in {"running", "paused", "stopped"}:
        fsm.mark_running("s")
    if setup == "paused":
        fsm.pause("s")
    if setup == "stopped":
        fsm.stop("s")
    if setup == "error":
        fsm.mark_error("s")
    assert fsm.shutdown("s") is State.SHUTDOWN


def test_shutdown_is_terminal() -> None:
    fsm = _running()
    fsm.shutdown("s")
    for command in (
        fsm.start,
        fsm.mark_running,
        fsm.pause,
        fsm.resume,
        fsm.stop,
        fsm.mark_error,
        fsm.shutdown,
    ):
        with pytest.raises(InvalidStrategyTransitionError):
            command("s")
    assert fsm.state_of("s") is State.SHUTDOWN


# --------------------------------------------------------------------------- #
# Isolation, snapshot, determinism
# --------------------------------------------------------------------------- #
def test_force_stop_isolates_other_strategies() -> None:
    fsm = StrategyLifecycle()
    for sid in ("a", "b"):
        fsm.register(sid)
        fsm.start(sid)
        fsm.mark_running(sid)
    fsm.pause("b")
    fsm.force_stop("a")
    assert fsm.state_of("a") is State.STOPPED
    assert fsm.state_of("b") is State.PAUSED


def test_error_isolates_other_strategies() -> None:
    fsm = StrategyLifecycle()
    for sid in ("a", "b"):
        fsm.register(sid)
        fsm.start(sid)
        fsm.mark_running(sid)
    fsm.mark_error("a")
    assert fsm.state_of("a") is State.ERROR
    assert fsm.state_of("b") is State.RUNNING


def test_snapshot_is_immutable_and_ordered() -> None:
    fsm = StrategyLifecycle()
    for sid in ("b", "a"):
        fsm.register(sid)
    snapshot = fsm.snapshot()
    assert isinstance(snapshot, tuple)
    assert all(isinstance(item, StrategyRuntimeStatus) for item in snapshot)
    assert [item.strategy_id for item in snapshot] == ["a", "b"]


def test_same_command_sequence_is_deterministic() -> None:
    def run() -> tuple[StrategyRuntimeStatus, ...]:
        fsm = StrategyLifecycle()
        for sid in ("x", "y"):
            fsm.register(sid)
            fsm.start(sid)
        fsm.mark_running("x")
        fsm.mark_error("y")
        return fsm.snapshot()

    assert run() == run()
