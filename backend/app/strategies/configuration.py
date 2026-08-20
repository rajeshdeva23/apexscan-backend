"""Immutable, versioned base for typed strategy configuration (P5.1; docs/07 §15).

Individual strategies subclass :class:`StrategyConfiguration` to add their own
typed, validated parameters. The base fixes only the shared identity seam — a
validated ``config_version`` — so configuration is versioned and tied to results
(docs/07 §9.3, §15) without a single universal config model or an untyped dict.
Configuration is immutable after validation; a strategy never mutates it.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

from app.strategies.models import FrozenModel

SemverLite = Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")]


class StrategyConfiguration(FrozenModel):
    """Immutable base for a strategy's validated configuration.

    Attributes:
        config_version: The configuration schema version (semver-lite), recorded on
            results for reproducibility (docs/07 §15; ADR-007 D11).
    """

    config_version: SemverLite
