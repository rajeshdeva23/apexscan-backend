"""Typed domain errors for the strategy registry (P5.2).

Framework misuse fails closed with an explicit, narrow error rather than leaking a
raw ``KeyError`` or silently succeeding. No HTTP, provider, or persistence concerns.
"""

from __future__ import annotations


class StrategyRegistryError(RuntimeError):
    """Base error for strategy-registry misuse."""


class InvalidStrategyError(StrategyRegistryError):
    """Raised when an object does not conform to the Strategy contract."""


class StrategyAlreadyRegisteredError(StrategyRegistryError):
    """Raised when a strategy_id is already present in the registry (fail closed)."""


class StrategyNotFoundError(StrategyRegistryError):
    """Raised when a lookup targets a strategy_id that is not registered."""
