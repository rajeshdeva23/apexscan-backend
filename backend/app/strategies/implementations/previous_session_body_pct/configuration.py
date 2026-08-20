"""Previous Session Body % strategy configuration (ADR-007 PSB spec PSB9).

The body mathematics are a fixed domain contract, not configuration. V1 is a pure
rank-all ranking feature with **no** strategy-specific tunable (every valid, ready
previous session matches). No threshold/window/percentile/z-score/direction/weight
fields exist here (PSB9).
"""

from __future__ import annotations

from app.strategies.configuration import StrategyConfiguration


class PreviousSessionBodyPctConfiguration(StrategyConfiguration):
    """Immutable, validated configuration for the Previous Session Body % strategy.

    Carries only the shared ``config_version`` — V1 has no strategy-specific parameter
    (rank-all, no threshold; PSB9).
    """
