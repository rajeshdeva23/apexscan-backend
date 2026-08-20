"""Previous Session Range % strategy configuration (ADR-007 PSR spec PSR9).

The range mathematics are a fixed domain contract, not configuration. V1 is a pure
rank-all ranking feature with **no** strategy-specific tunable (every valid, ready
previous session matches). No window/percentile/z-score/ATR/volume/VWAP/threshold/
direction/weight fields exist here (PSR9/PSR12).
"""

from __future__ import annotations

from app.strategies.configuration import StrategyConfiguration


class PreviousSessionRangePctConfiguration(StrategyConfiguration):
    """Immutable, validated configuration for the Previous Session Range % strategy.

    Carries only the shared ``config_version`` — V1 has no strategy-specific parameter
    (rank-all, no threshold; PSR8/PSR9).
    """
