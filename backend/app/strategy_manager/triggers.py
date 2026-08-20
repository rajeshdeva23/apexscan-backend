"""Pure trigger-relevance detection from MarketContext deltas (P5.3).

Decides whether a strategy's declared :class:`StrategyTrigger` is relevant to a
context update, comparing the current context to the manager-held previous context
for that instrument. ``ON_CONTEXT`` is always relevant; the others require concrete
evidence of the specific change (a value delta, a new authoritative finalized
candle, a session-state change, or a historical-ready transition). No strategy names,
no market logic, no side effects.
"""

from __future__ import annotations

from datetime import datetime

from app.market_engine.context import MarketContext
from app.strategies.enums import StrategyTrigger


def is_triggered(
    trigger: StrategyTrigger, *, previous: MarketContext | None, current: MarketContext
) -> bool:
    """Return whether ``trigger`` is relevant given the previous→current context delta.

    Args:
        trigger: The strategy's declared trigger.
        previous: The last context the manager processed for this instrument, if any.
        current: The context now being processed.

    Returns:
        Whether the strategy should be considered for evaluation this cycle.
    """
    if trigger is StrategyTrigger.ON_CONTEXT:
        return True
    if trigger is StrategyTrigger.ON_TICK:
        return current.latest_tick is not None and (
            previous is None or current.latest_tick != previous.latest_tick
        )
    if trigger is StrategyTrigger.ON_QUOTE:
        return current.latest_quote is not None and (
            previous is None or current.latest_quote != previous.latest_quote
        )
    if trigger is StrategyTrigger.ON_CANDLE_FINALIZED:
        return _has_new_finalized(previous, current)
    if trigger is StrategyTrigger.ON_SESSION_TRANSITION:
        return _session_changed(previous, current)
    return _historical_became_ready(previous, current)  # ON_HISTORICAL_READY


def _finalized_identities(context: MarketContext) -> frozenset[tuple[str, datetime, datetime]]:
    return frozenset(
        (candles.timeframe.label, candle.start_timestamp, candle.end_timestamp)
        for candles in context.candle_sets
        for candle in candles.finalized
    )


def _has_new_finalized(previous: MarketContext | None, current: MarketContext) -> bool:
    prior = _finalized_identities(previous) if previous is not None else frozenset()
    return bool(_finalized_identities(current) - prior)


def _session_changed(previous: MarketContext | None, current: MarketContext) -> bool:
    if previous is None or previous.session is None or current.session is None:
        return False  # no transition can be proven from nothing (initial context)
    return previous.session.market_state != current.session.market_state


def _historical_became_ready(previous: MarketContext | None, current: MarketContext) -> bool:
    if current.historical is None:
        return False
    return previous is None or previous.historical is None
