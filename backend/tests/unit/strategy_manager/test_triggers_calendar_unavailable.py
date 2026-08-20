"""Trigger-relevance tolerance for the CALENDAR_UNAVAILABLE session state (P5.3).

The ``ON_SESSION_TRANSITION`` detector compares ``market_state`` with ``!=`` and treats
``CALENDAR_UNAVAILABLE`` as an ordinary distinct enum value (ADR-011 live out-of-coverage
addendum LC12): a transition into or out of it is a benign detected change; no branch
assumes enum-exhaustiveness or treats a non-HOLIDAY state as trading.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.market_engine.context import MarketContext, MarketState, SessionContext
from app.schemas.market_data import Instrument, Tick
from app.strategies.enums import StrategyTrigger
from app.strategy_manager.triggers import is_triggered

_INSTRUMENT = Instrument(exchange="NSE", symbol="RELIANCE")
_TS = datetime(2026, 1, 1, 6, 30, tzinfo=UTC)


def _context(state: MarketState, *, version: int) -> MarketContext:
    session = SessionContext(
        trading_date=date(2026, 1, 1), market_state=state, exchange_timezone="Asia/Kolkata"
    )
    tick = Tick(
        instrument=_INSTRUMENT, event_timestamp=_TS, last_price=Decimal(1), traded_quantity=1
    )
    return MarketContext(
        instrument=_INSTRUMENT,
        version=version,
        sequence=version,
        event_timestamp=_TS,
        observed_at=_TS,
        latest_tick=tick,
        session=session,
    )


def test_transition_into_calendar_unavailable_is_detected() -> None:
    previous = _context(MarketState.LIVE_SESSION, version=1)
    current = _context(MarketState.CALENDAR_UNAVAILABLE, version=2)
    assert is_triggered(StrategyTrigger.ON_SESSION_TRANSITION, previous=previous, current=current)


def test_no_transition_when_calendar_unavailable_is_stable() -> None:
    previous = _context(MarketState.CALENDAR_UNAVAILABLE, version=1)
    current = _context(MarketState.CALENDAR_UNAVAILABLE, version=2)
    assert not is_triggered(
        StrategyTrigger.ON_SESSION_TRANSITION, previous=previous, current=current
    )
