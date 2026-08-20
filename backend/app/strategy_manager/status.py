"""Immutable per-strategy runtime lifecycle status (P5.2).

A small, deterministic value object for snapshotting the lifecycle tracker. It
carries only the strategy id and its current runtime state — no descriptor, no
strategy instance, no timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.strategies.enums import StrategyLifecycleState


@dataclass(frozen=True, slots=True)
class StrategyRuntimeStatus:
    """The runtime lifecycle state of one strategy at snapshot time.

    Attributes:
        strategy_id: The strategy's stable identity.
        state: Its current runtime lifecycle state.
    """

    strategy_id: str
    state: StrategyLifecycleState
