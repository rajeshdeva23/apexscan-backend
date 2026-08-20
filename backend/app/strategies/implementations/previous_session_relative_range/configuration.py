"""Previous Session Relative Range strategy configuration (ADR-007 PSRR spec PSRR11).

The baseline session count is a fixed governed constant (20), not configuration: the
``Strategy.requirements`` property is static and cannot depend on the configuration
instance, so a configurable baseline could not flow into the declared
``HistoricalRequirement`` (PSRR5). V1 therefore exposes only the shared ``config_version``;
no ``baseline_sessions``/threshold/direction/weight field.
"""

from __future__ import annotations

from app.strategies.configuration import StrategyConfiguration


class PreviousSessionRelativeRangeConfiguration(StrategyConfiguration):
    """Immutable, validated configuration for the Previous Session Relative Range strategy.

    Carries only the shared ``config_version`` — V1 has no strategy-specific parameter
    (fixed baseline of 20 sessions; PSRR5/PSRR11).
    """
