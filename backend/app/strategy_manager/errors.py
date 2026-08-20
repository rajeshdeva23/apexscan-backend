"""Typed domain errors for the strategy lifecycle FSM (P5.2).

Illegal lifecycle use fails closed with an explicit error — never a silent no-op,
a returned ``False``, or an auto-corrected transition. Errors carry only safe
identifying context (strategy id, current state, attempted command); no secrets,
stack traces, provider data, or mutable strategy objects.
"""

from __future__ import annotations

from app.strategies.enums import StrategyLifecycleState


class StrategyLifecycleError(RuntimeError):
    """Base error for strategy lifecycle misuse."""


class StrategyNotTrackedError(StrategyLifecycleError):
    """Raised when a lifecycle command targets an untracked strategy_id."""


class StrategyAlreadyTrackedError(StrategyLifecycleError):
    """Raised when a strategy_id is registered for lifecycle tracking twice."""


class InvalidStrategyTransitionError(StrategyLifecycleError):
    """Raised when a lifecycle command is illegal from the current state.

    Attributes:
        strategy_id: The strategy the command targeted.
        current_state: The state the strategy was in (unchanged after the error).
        command: The rejected lifecycle command.
    """

    def __init__(
        self, *, strategy_id: str, current_state: StrategyLifecycleState, command: str
    ) -> None:
        """Record the safe transition context and build a clear message."""
        self.strategy_id = strategy_id
        self.current_state = current_state
        self.command = command
        super().__init__(
            f"command {command!r} is not permitted from state "
            f"{current_state.value!r} for strategy {strategy_id!r}"
        )
