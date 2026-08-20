"""Manager-owned ranking of strategy results for presentation (P5.5; docs/07 §14).

Ranking is *presentation ordering* — it never re-computes or overrides a
strategy-owned score (docs/07 §14.5, rules 8/19; ADR-007 D11). The rank is **not**
part of :class:`StrategyResult` equality (a result must not change because another
instrument's score moved, D11); a separate immutable :class:`RankedStrategyResult`
projection carries the ordinal. Ranking covers only ``MATCHED`` results that carry a
score: a missing score excludes a match from ranking (docs/07 §11 Score Generation),
and ``NO_MATCH``/``SKIPPED``/``ERROR`` never receive a rank. Ordering is deterministic
— descending score, then ascending ``strategy_id`` as the stable tie-break (docs/07
§14.3/§14.4).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from pydantic import Field

from app.strategies.enums import EvaluationStatus
from app.strategies.models import FrozenModel
from app.strategies.results import StrategyResult

_FIRST_RANK = 1


class RankedStrategyResult(FrozenModel):
    """An immutable (rank, result) projection — presentation ordering only.

    Attributes:
        rank: The 1-based presentation position (rank 1 is the strongest score).
        result: The immutable result being ordered; unchanged by ranking.
    """

    rank: int = Field(ge=_FIRST_RANK)
    result: StrategyResult


def rank_results(results: Sequence[StrategyResult]) -> tuple[RankedStrategyResult, ...]:
    """Order the rankable results of one cycle into a deterministic ranked projection.

    Only ``MATCHED`` results that carry a score are ranked; everything else is
    excluded (never fake-ranked). Ties on score are broken by ascending
    ``strategy_id`` so the ordering is total and reproducible.

    Args:
        results: The cycle's emitted results (a per-MarketContext-version set).

    Returns:
        The ranked projection, rank 1 first; empty when nothing is rankable.
    """
    rankable: list[tuple[Decimal, str, StrategyResult]] = [
        (result.score, result.strategy_id, result)
        for result in results
        if result.status is EvaluationStatus.MATCHED and result.score is not None
    ]
    ordered = sorted(rankable, key=lambda item: (-item[0], item[1]))
    return tuple(
        RankedStrategyResult(rank=index, result=item[2])
        for index, item in enumerate(ordered, start=_FIRST_RANK)
    )
