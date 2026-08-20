"""Immutable strategy evaluation and result contracts (P5.1; docs/07 §12).

``StrategyEvaluation`` is the internal outcome of one evaluation call.
``StrategyResult`` is the standardized, immutable, self-describing external fact
(docs/07 §12.1/§12.4): it records the strategy identity + version, the config
version, and the exact MarketContext version it interpreted, so it is fully
reproducible. Results are immutable — a correction is a new result for a new
context version, never an edit (docs/07 §12.3). Ranking is **not** part of the
result (ADR-007 D11); operational timing/execution stats are tracked by the
manager, outside this deterministic contract.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.schemas.market_data import Instrument
from app.strategies.descriptor import SemverLite, StrategyId
from app.strategies.enums import EvaluationStatus
from app.strategies.models import FrozenModel, require_utc

ReasonCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]
MetricName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")]
_INITIAL_CONTEXT_VERSION = 1


class MetricEntry(FrozenModel):
    """One structured, serialization-safe strategy metric (name → primitive value)."""

    name: MetricName
    value: Decimal | int | str | bool


def _validate_reason_codes(codes: tuple[str, ...]) -> tuple[str, ...]:
    if len(set(codes)) != len(codes):
        raise ValueError("reason codes must not contain duplicates")
    return codes


def _validate_metrics(metrics: tuple[MetricEntry, ...]) -> tuple[MetricEntry, ...]:
    names = [metric.name for metric in metrics]
    if len(set(names)) != len(names):
        raise ValueError("metric names must be unique")
    return tuple(sorted(metrics, key=lambda metric: metric.name))


class StrategyEvaluation(FrozenModel):
    """The internal, immutable outcome of a single strategy evaluation call.

    Carries only the per-call outcome plus the facts it keyed on; the manager stamps
    identity, versions, and timestamp when promoting it to a :class:`StrategyResult`.
    """

    instrument: Instrument
    context_version: int = Field(ge=_INITIAL_CONTEXT_VERSION)
    status: EvaluationStatus
    score: Decimal | None = None
    confidence: Decimal | None = None
    reason_codes: tuple[ReasonCode, ...] = ()
    metrics: tuple[MetricEntry, ...] = ()
    diagnostics: tuple[tuple[str, str], ...] = ()

    _validate_reason_codes = field_validator("reason_codes")(_validate_reason_codes)
    _validate_metrics = field_validator("metrics")(_validate_metrics)

    @model_validator(mode="after")
    def _matched_requires_reasons(self) -> StrategyEvaluation:
        if self.status is EvaluationStatus.MATCHED and not self.reason_codes:
            raise ValueError("a matched evaluation must carry at least one reason code")
        return self


class StrategyResult(FrozenModel):
    """The standardized, immutable, self-describing external strategy result.

    Attributes:
        result_id: Optional identity supplied by the manager (never randomly
            generated inside a pure evaluation).
        strategy_id: The producing strategy's stable identity.
        strategy_version: The strategy version that evaluated (reproducibility).
        config_version: The configuration version applied.
        instrument: The canonical instrument the result is about.
        context_version: The exact MarketContext version interpreted.
        evaluation_timestamp: When the evaluation occurred (tz-aware UTC).
        status: The evaluation outcome.
        score: Optional strategy-owned match strength (no governed scale).
        confidence: Optional strategy-owned confidence (distinct from score).
        reason_codes: Structured explainability (mandatory for a match).
        metrics: Structured strategy-specific metrics.
        metadata: Immutable descriptive provenance key/value pairs.
        diagnostics: Optional sanitized diagnostic key/value pairs (no traces).
    """

    result_id: str | None = None
    strategy_id: StrategyId
    strategy_version: SemverLite
    config_version: SemverLite
    instrument: Instrument
    context_version: int = Field(ge=_INITIAL_CONTEXT_VERSION)
    evaluation_timestamp: datetime
    status: EvaluationStatus
    score: Decimal | None = None
    confidence: Decimal | None = None
    reason_codes: tuple[ReasonCode, ...] = ()
    metrics: tuple[MetricEntry, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()
    diagnostics: tuple[tuple[str, str], ...] = ()

    _validate_timestamp = field_validator("evaluation_timestamp")(require_utc)
    _validate_reason_codes = field_validator("reason_codes")(_validate_reason_codes)
    _validate_metrics = field_validator("metrics")(_validate_metrics)

    @model_validator(mode="after")
    def _matched_requires_reasons(self) -> StrategyResult:
        if self.status is EvaluationStatus.MATCHED and not self.reason_codes:
            raise ValueError("a matched result must carry at least one reason code")
        return self
