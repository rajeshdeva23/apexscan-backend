"""Promotion of an internal evaluation into the external StrategyResult (P5.5).

``StrategyEvaluation`` is the internal per-call outcome (P5.1); ``StrategyResult``
is the immutable, self-describing external fact (docs/07 §12; ADR-007 D10). Promotion
is a pure, deterministic *stamping* step owned by the Strategy Manager (docs/07
§12.2): it copies the strategy's typed outcome verbatim and attaches identity, the
strategy/config versions, and the manager-supplied evaluation instant. It never
re-runs ``evaluate`` and never recomputes or normalises the strategy-owned score
(docs/07 §13; rules 8/19). No identity is invented — ``result_id`` stays ``None``
because durable identity belongs to persistence (docs/02 §6.4), a later slice.
"""

from __future__ import annotations

from datetime import datetime

from app.strategies.descriptor import StrategyDescriptor
from app.strategies.results import StrategyEvaluation, StrategyResult


def promote_evaluation(
    *,
    evaluation: StrategyEvaluation,
    descriptor: StrategyDescriptor,
    config_version: str,
    evaluation_timestamp: datetime,
) -> StrategyResult:
    """Stamp an internal evaluation into an immutable external :class:`StrategyResult`.

    The strategy's typed outcome — ``status``, ``score``, ``confidence``,
    ``reason_codes``, ``metrics``, ``diagnostics`` — is carried through unchanged;
    nothing is re-evaluated, re-scored, or re-normalised. Identity and versions come
    from the descriptor and the applied configuration; the timestamp is the manager's
    deterministic build instant (never the wall clock).

    Args:
        evaluation: The strategy's internal evaluation outcome for one context.
        descriptor: The producing strategy's immutable identity and static metadata.
        config_version: The version of the configuration that was applied.
        evaluation_timestamp: The deterministic evaluation instant (tz-aware UTC),
            supplied by the manager from the MarketContext's ``observed_at``.

    Returns:
        The immutable, rank-free :class:`StrategyResult`.
    """
    return StrategyResult(
        result_id=None,
        strategy_id=descriptor.strategy_id,
        strategy_version=descriptor.version,
        config_version=config_version,
        instrument=evaluation.instrument,
        context_version=evaluation.context_version,
        evaluation_timestamp=evaluation_timestamp,
        status=evaluation.status,
        score=evaluation.score,
        confidence=evaluation.confidence,
        reason_codes=evaluation.reason_codes,
        metrics=evaluation.metrics,
        metadata=(("category", descriptor.category.value),),
        diagnostics=evaluation.diagnostics,
    )
