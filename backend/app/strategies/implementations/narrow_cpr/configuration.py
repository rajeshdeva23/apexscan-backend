"""Narrow CPR strategy configuration (ADR-007 Narrow CPR spec NCR18).

The CPR mathematics are a fixed domain contract, not configuration. The only V1
tunable is an optional narrowness threshold used to gate ``MATCHED`` vs ``NO_MATCH``;
when it is ``None`` the strategy is a pure ranking feature that matches every valid,
ready previous session (NCR11). No V2 fields (history window, percentile, z-score) and
no directional/weight fields exist here (NCR6/NCR12/NCR29).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from app.strategies.configuration import StrategyConfiguration


class NarrowCprConfiguration(StrategyConfiguration):
    """Immutable, validated configuration for the Narrow CPR strategy.

    Attributes:
        narrow_cpr_max_width_pct: Optional strictly-positive threshold on the
            pivot-normalised CPR width percentage. ``None`` (default) means no filter —
            every valid previous session matches (pure ranking feature). When set,
            ``cpr_width_pct <= threshold`` matches and a wider CPR does not (NCR7/NCR11).
    """

    narrow_cpr_max_width_pct: Decimal | None = Field(default=None, gt=0)
