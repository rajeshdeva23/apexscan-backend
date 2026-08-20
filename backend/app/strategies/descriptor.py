"""Immutable static metadata identifying a strategy (P5.1; docs/07 §9.2).

The descriptor is the strategy's stable identity and static metadata — never its
runtime lifecycle status (that is manager-owned, ADR-007). ``strategy_id`` is the
machine-safe stable identity used everywhere; ``display_name`` is independent human
text. Versions are validated semver-lite (docs/07 mandates no specific scheme, so
the smallest safe validated form is used).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from app.strategies.enums import EmissionPolicy, StrategyCategory
from app.strategies.models import FrozenModel

# Stable, machine-safe strategy identity: lowercase snake, letter-led.
StrategyId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
# Semver-lite MAJOR.MINOR.PATCH — explicit, validated, not free display text.
SemverLite = Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")]

_INITIAL_CONTEXT_VERSION = 1


class StrategyDescriptor(FrozenModel):
    """Immutable identity and static metadata for one strategy.

    Attributes:
        strategy_id: Stable, machine-safe unique identity (docs/07 §9.2).
        display_name: Human-facing name, independent of identity.
        description: Short human description for discovery/display.
        version: The strategy implementation version (semver-lite).
        category: The conceptual family (docs/07 §8).
        emission_policy: How this strategy's results are emitted (ADR-007 D10).
        min_context_version: Lowest MarketContext version this strategy supports.
        max_context_version: Highest supported version, or ``None`` for open-ended
            additive compatibility (docs/07 §4.9).
    """

    strategy_id: StrategyId
    display_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    version: SemverLite
    category: StrategyCategory
    emission_policy: EmissionPolicy
    min_context_version: int = Field(default=_INITIAL_CONTEXT_VERSION, ge=_INITIAL_CONTEXT_VERSION)
    max_context_version: int | None = Field(default=None, ge=_INITIAL_CONTEXT_VERSION)

    @model_validator(mode="after")
    def _validate_context_range(self) -> StrategyDescriptor:
        if (
            self.max_context_version is not None
            and self.max_context_version < self.min_context_version
        ):
            raise ValueError("max_context_version must not be less than min_context_version")
        return self
