"""Shared immutable-model base for the Strategy contract layer (P5.1).

Every Strategy value object is strict, frozen, and forbids unknown fields — the
same discipline the Market Engine's canonical models use, so strategy contracts
are immutable, deterministic, and safe to share read-only across the engine and
(later) the Strategy Manager. No orchestration or I/O lives here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

_STRATEGY_MODEL_CONFIG = ConfigDict(
    arbitrary_types_allowed=True,  # carry stdlib Timeframe / HistoricalRequirement values
    extra="forbid",
    frozen=True,
    hide_input_in_errors=True,
    strict=True,
)


class FrozenModel(BaseModel):
    """Strict, immutable, extra-forbidding base for all strategy value objects."""

    model_config = _STRATEGY_MODEL_CONFIG


def require_utc(value: datetime) -> datetime:
    """Reject a naive timestamp and normalise an aware one to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)
