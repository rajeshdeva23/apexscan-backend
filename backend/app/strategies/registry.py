"""The static, generic Strategy registry (P5.2; docs/07 §9).

The registry is the application-composition catalog of available strategy plug-ins.
It is fully generic (Open-Closed): it depends only on the P5.1 :class:`Strategy`
contract and never on any concrete strategy — adding a strategy is registration, not
a registry change. ``descriptor.strategy_id`` is the authoritative, stable key,
captured once at registration. There is no dynamic import, filesystem discovery,
or user-supplied code — registration is explicit and static. The registry answers
"what strategies exist"; it holds no runtime lifecycle state (ADR-007; that is the
lifecycle FSM's concern).
"""

from __future__ import annotations

from app.strategies.contracts import Strategy
from app.strategies.errors import (
    InvalidStrategyError,
    StrategyAlreadyRegisteredError,
    StrategyNotFoundError,
)


class StrategyRegistry:
    """A deterministic, generic catalog of registered strategy plug-ins."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._strategies: dict[str, Strategy] = {}

    def register(self, strategy: Strategy) -> None:
        """Register a strategy under its descriptor's ``strategy_id``.

        The descriptor is authoritative for identity — there is no separate id
        argument that could disagree. The exact instance is retained.

        Args:
            strategy: A conforming :class:`Strategy` implementation.

        Raises:
            InvalidStrategyError: If ``strategy`` does not satisfy the Strategy
                contract (a shallow ``runtime_checkable`` check — it confirms the
                required members exist, not their exact signatures).
            StrategyAlreadyRegisteredError: If the ``strategy_id`` is already
                registered (duplicates fail closed; never last-write-wins).
        """
        if not isinstance(strategy, Strategy):
            raise InvalidStrategyError("object does not conform to the Strategy contract")
        strategy_id = strategy.descriptor.strategy_id
        if strategy_id in self._strategies:
            raise StrategyAlreadyRegisteredError(strategy_id)
        self._strategies[strategy_id] = strategy

    def get(self, strategy_id: str) -> Strategy:
        """Return the registered strategy for an id, or fail with a typed error.

        Args:
            strategy_id: The stable identity to look up.

        Returns:
            The exact registered :class:`Strategy` instance.

        Raises:
            StrategyNotFoundError: If no strategy is registered under ``strategy_id``.
        """
        try:
            return self._strategies[strategy_id]
        except KeyError as error:
            raise StrategyNotFoundError(strategy_id) from error

    def contains(self, strategy_id: str) -> bool:
        """Return whether a strategy is registered under ``strategy_id``."""
        return strategy_id in self._strategies

    def identifiers(self) -> tuple[str, ...]:
        """Return the registered strategy ids in ascending, deterministic order."""
        return tuple(sorted(self._strategies))

    def strategies(self) -> tuple[Strategy, ...]:
        """Return an immutable snapshot of registered strategies, ordered by id."""
        return tuple(self._strategies[strategy_id] for strategy_id in sorted(self._strategies))
