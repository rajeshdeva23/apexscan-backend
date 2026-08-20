"""The Strategy Protocol and evaluation metadata (P5.1; docs/07 §7).

Defines the one broker-neutral contract every strategy conforms to: given a
read-only :class:`MarketContext`, a validated configuration, and orchestration
metadata, a strategy returns a :class:`StrategyEvaluation`. The engine depends on
this contract, never on a strategy's internals (docs/07 §7.1, rule 30). No
provider, event bus, database, or mutation engine is referenced — a strategy is a
pure evaluation unit (docs/07 §4.11).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol, runtime_checkable

from pydantic import Field, field_validator

from app.market_engine.context import MarketContext
from app.strategies.configuration import StrategyConfiguration
from app.strategies.descriptor import StrategyDescriptor
from app.strategies.enums import StrategyTrigger
from app.strategies.models import FrozenModel, require_utc
from app.strategies.requirements import StrategyRequirements
from app.strategies.results import StrategyEvaluation

_INITIAL_CONTEXT_VERSION = 1


class StrategyEvaluationMetadata(FrozenModel):
    """Immutable orchestration context for one evaluation (not market facts).

    Attributes:
        trigger: The market event that triggered this evaluation.
        context_version: The MarketContext version being evaluated.
        observed_at: The manager-supplied build instant (tz-aware UTC) — strategies
            never read the wall clock.
        trading_date: The exchange-local trading date, when available.
    """

    trigger: StrategyTrigger
    context_version: int = Field(ge=_INITIAL_CONTEXT_VERSION)
    observed_at: datetime
    trading_date: date | None = None

    _validate_observed_at = field_validator("observed_at")(require_utc)


@runtime_checkable
class Strategy(Protocol):
    """The shared, broker-neutral contract every strategy conforms to (docs/07 §7)."""

    @property
    def descriptor(self) -> StrategyDescriptor:
        """Return the strategy's immutable identity and static metadata."""
        ...

    @property
    def requirements(self) -> StrategyRequirements:
        """Return the strategy's declared data/fact requirements."""
        ...

    @property
    def configuration_type(self) -> type[StrategyConfiguration]:
        """Return the concrete configuration type this strategy validates against."""
        ...

    def evaluate(
        self,
        context: MarketContext,
        configuration: StrategyConfiguration,
        metadata: StrategyEvaluationMetadata,
    ) -> StrategyEvaluation:
        """Interpret one read-only MarketContext into a StrategyEvaluation (pure, no I/O)."""
        ...
