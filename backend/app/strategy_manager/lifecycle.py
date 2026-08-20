"""The strategy lifecycle state machine (P5.2; ADR-007 D2, corrected).

Runtime lifecycle state owned by the Strategy Manager layer — never stored in the
descriptor, the registry, or the strategy implementation, and distinct from the
durable enabled/disabled intent (persistence is a later phase). Each ``strategy_id``
has an independent state; a command on one strategy never affects another. The FSM
is the only mutation path: intent-revealing commands validate the corrected ADR-007
transition table and fail closed on anything unlisted. Pure and deterministic — no
wall clock, randomness, I/O, or async scheduling.
"""

from __future__ import annotations

from app.strategies.enums import StrategyLifecycleState as State
from app.strategy_manager.errors import (
    InvalidStrategyTransitionError,
    StrategyAlreadyTrackedError,
    StrategyNotTrackedError,
)
from app.strategy_manager.status import StrategyRuntimeStatus

# Command → (permitted source states, target state) for the plain transitions.
# force_stop and shutdown carry extra conditions and are handled explicitly.
_COMMAND_TRANSITIONS: dict[str, tuple[frozenset[State], State]] = {
    "start": (frozenset({State.REGISTERED, State.STOPPED, State.ERROR}), State.STARTING),
    "mark_running": (frozenset({State.STARTING}), State.RUNNING),
    "pause": (frozenset({State.RUNNING}), State.PAUSED),
    "resume": (frozenset({State.PAUSED}), State.RUNNING),
    "stop": (frozenset({State.RUNNING, State.PAUSED, State.ERROR}), State.STOPPED),
    "mark_error": (frozenset({State.STARTING, State.RUNNING}), State.ERROR),
}

# Force-stop is authorized unconditionally from these; STARTING requires an explicit
# clean-cancellable proof from the caller (ADR-007 D6). STARTING is NOT a sub-state.
_FORCE_STOP_SOURCES: frozenset[State] = frozenset({State.RUNNING, State.PAUSED, State.ERROR})


class StrategyLifecycle:
    """Tracks and validates each strategy's independent runtime lifecycle state."""

    def __init__(self) -> None:
        """Create an empty lifecycle tracker."""
        self._states: dict[str, State] = {}

    def register(self, strategy_id: str) -> State:
        """Begin lifecycle tracking for a strategy in the initial REGISTERED state.

        Args:
            strategy_id: The strategy's stable identity.

        Returns:
            The initial state (``REGISTERED``).

        Raises:
            StrategyAlreadyTrackedError: If the strategy is already tracked (never
                resets an existing, possibly-running, entry).
        """
        if strategy_id in self._states:
            raise StrategyAlreadyTrackedError(strategy_id)
        self._states[strategy_id] = State.REGISTERED
        return State.REGISTERED

    def state_of(self, strategy_id: str) -> State:
        """Return the current runtime state, or fail if the strategy is untracked."""
        return self._require(strategy_id)

    def snapshot(self) -> tuple[StrategyRuntimeStatus, ...]:
        """Return an immutable, id-ordered snapshot of all tracked lifecycle states."""
        return tuple(
            StrategyRuntimeStatus(strategy_id=strategy_id, state=self._states[strategy_id])
            for strategy_id in sorted(self._states)
        )

    def start(self, strategy_id: str) -> State:
        """Begin startup (REGISTERED / STOPPED / ERROR → STARTING). No side effects here."""
        return self._apply(strategy_id, "start")

    def mark_running(self, strategy_id: str) -> State:
        """Mark startup complete (STARTING → RUNNING)."""
        return self._apply(strategy_id, "mark_running")

    def pause(self, strategy_id: str) -> State:
        """Pause a running strategy (RUNNING → PAUSED); requirements are retained."""
        return self._apply(strategy_id, "pause")

    def resume(self, strategy_id: str) -> State:
        """Resume a paused strategy (PAUSED → RUNNING); no STARTING pass-through."""
        return self._apply(strategy_id, "resume")

    def stop(self, strategy_id: str) -> State:
        """Normal stop (RUNNING / PAUSED / ERROR → STOPPED); strategy stays registered."""
        return self._apply(strategy_id, "stop")

    def mark_error(self, strategy_id: str) -> State:
        """Fault a strategy (STARTING / RUNNING → ERROR)."""
        return self._apply(strategy_id, "mark_error")

    def force_stop(self, strategy_id: str, *, clean_cancellable: bool = False) -> State:
        """Force a strategy to STOPPED (ADR-007 D6), distinct from a normal stop.

        Valid from RUNNING/PAUSED/ERROR unconditionally, and from STARTING only when
        the caller explicitly proves the startup is clean-cancellable. The default is
        fail-closed: force-stopping STARTING without proof is rejected.

        Args:
            strategy_id: The strategy to force-stop.
            clean_cancellable: Whether a STARTING strategy's startup work is proven
                safely cancellable by the caller (a future-orchestrator precondition).

        Returns:
            The new state (``STOPPED``).

        Raises:
            InvalidStrategyTransitionError: If force-stop is not permitted from the
                current state.
        """
        current = self._require(strategy_id)
        permitted = current in _FORCE_STOP_SOURCES or (
            current is State.STARTING and clean_cancellable
        )
        if not permitted:
            raise InvalidStrategyTransitionError(
                strategy_id=strategy_id, current_state=current, command="force_stop"
            )
        self._states[strategy_id] = State.STOPPED
        return State.STOPPED

    def shutdown(self, strategy_id: str) -> State:
        """Terminalize a strategy (any non-SHUTDOWN state → SHUTDOWN); not idempotent."""
        current = self._require(strategy_id)
        if current is State.SHUTDOWN:
            raise InvalidStrategyTransitionError(
                strategy_id=strategy_id, current_state=current, command="shutdown"
            )
        self._states[strategy_id] = State.SHUTDOWN
        return State.SHUTDOWN

    def _apply(self, strategy_id: str, command: str) -> State:
        current = self._require(strategy_id)
        permitted_sources, target = _COMMAND_TRANSITIONS[command]
        if current not in permitted_sources:
            raise InvalidStrategyTransitionError(
                strategy_id=strategy_id, current_state=current, command=command
            )
        self._states[strategy_id] = target
        return target

    def _require(self, strategy_id: str) -> State:
        try:
            return self._states[strategy_id]
        except KeyError:
            raise StrategyNotTrackedError(strategy_id) from None
