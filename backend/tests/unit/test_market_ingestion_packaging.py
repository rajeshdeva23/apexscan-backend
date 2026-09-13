"""Long-lived packaging readiness for the market-ingestion service (DECOUPLING PHASE H3C).

Unit-level proof that the dynamic trading-date source follows the exchange-local session across
midnight/weekend/holiday boundaries WITHOUT a restart, by reusing the canonical
:class:`MarketSessionClassifier` (the market-IPC trading-date authority) rather than a wall-clock
date. A server/UTC midnight must never shift the trading date; only an exchange-local one does.
NO real Dhan, NO Redis, NO consumer, NO C1, NO backend TickEngine.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from app.market_engine.session import MarketSessionClassifier, SessionSchedule, TradingCalendar
from app.market_ingestion.publication import SessionTradingDate


def _classifier(*, holidays: tuple[date, ...] = ()) -> MarketSessionClassifier:
    """A canonical NSE-shaped session classifier (Asia/Kolkata), optionally with closed dates."""
    return MarketSessionClassifier(
        schedule=SessionSchedule(
            pre_open_start=time(9, 0),
            opening_auction_start=time(9, 8),
            regular_open=time(9, 15),
            regular_close=time(15, 30),
            closing_end=time(15, 40),
        ),
        calendar=TradingCalendar(holidays=list(holidays)),
        exchange_timezone="Asia/Kolkata",
    )


class _Clock:
    """A mutable UTC clock for driving the dynamic trading-date source deterministically."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment


def test_trading_date_is_exchange_local_not_utc() -> None:
    # 2026-09-13 20:00 UTC is 2026-09-14 01:30 IST → the trading date is the exchange-local day.
    clock = _Clock(datetime(2026, 9, 13, 20, 0, tzinfo=UTC))
    source = SessionTradingDate(classify=_classifier().classify, now=clock.now)
    assert source.current_trading_date() == date(2026, 9, 14)


def test_trading_date_rolls_at_exchange_local_midnight() -> None:
    # Before vs after IST midnight: re-evaluated per call, rolls D1 → D2 with no restart.
    clock = _Clock(datetime(2026, 9, 14, 18, 0, tzinfo=UTC))  # 23:30 IST on 09-14
    source = SessionTradingDate(classify=_classifier().classify, now=clock.now)
    assert source.current_trading_date() == date(2026, 9, 14)
    clock.moment = datetime(2026, 9, 14, 19, 0, tzinfo=UTC)  # 00:30 IST on 09-15 (post midnight)
    assert source.current_trading_date() == date(2026, 9, 15)


def test_utc_midnight_alone_does_not_change_trading_date() -> None:
    # Crossing 00:00 UTC (05:30 IST) must NOT change the exchange-local date (ADR-011 / §13).
    clock = _Clock(datetime(2026, 9, 14, 20, 0, tzinfo=UTC))  # 09-15 01:30 IST
    source = SessionTradingDate(classify=_classifier().classify, now=clock.now)
    before = source.current_trading_date()
    # past 00:00 UTC (08:30 IST) — the exchange-local date is still 09-15
    clock.moment = datetime(2026, 9, 15, 3, 0, tzinfo=UTC)
    assert source.current_trading_date() == before == date(2026, 9, 15)


def test_trading_date_evaluated_live_per_call() -> None:
    # §35.6: the publisher reads this per event, so each call reflects the live clock.
    clock = _Clock(datetime(2026, 9, 15, 6, 0, tzinfo=UTC))  # 11:30 IST 09-15 (mid-session)
    source = SessionTradingDate(classify=_classifier().classify, now=clock.now)
    first = source.current_trading_date()
    clock.moment = datetime(2026, 9, 16, 5, 0, tzinfo=UTC)  # next day 10:30 IST
    second = source.current_trading_date()
    assert first == date(2026, 9, 15)
    assert second == date(2026, 9, 16)  # re-evaluated live, never captured once at compose time


def test_weekend_and_holiday_dates_follow_the_calendar_authority() -> None:
    # Across trading day → holiday → next session the source returns each exchange-local date
    # (never a stale/wall date); the classifier marks the closure via market_state, and the
    # trading_date is still that canonical exchange-local day.
    holiday = date(2026, 9, 17)  # a declared exchange closure (Thursday)
    classifier = _classifier(holidays=(holiday,))
    clock = _Clock(datetime(2026, 9, 16, 6, 0, tzinfo=UTC))  # Wed 09-16, 11:30 IST (trading)
    source = SessionTradingDate(classify=classifier.classify, now=clock.now)
    assert source.current_trading_date() == date(2026, 9, 16)
    clock.moment = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)  # the holiday
    assert source.current_trading_date() == holiday  # canonical exchange-local date, not invented
    clock.moment = datetime(2026, 9, 18, 6, 0, tzinfo=UTC)  # the next session, 09-18
    assert source.current_trading_date() == date(2026, 9, 18)


def test_holiday_is_reflected_in_session_state_but_not_the_date() -> None:
    # Confirms the authority reuse: the classifier (not this source) owns the calendar; on a
    # holiday the market_state is HOLIDAY while the trading_date remains the exchange-local date.
    from app.market_engine.context import MarketState

    holiday = date(2026, 9, 17)
    classifier = _classifier(holidays=(holiday,))
    context = classifier.classify(datetime(2026, 9, 17, 6, 0, tzinfo=UTC))
    assert context.market_state is MarketState.HOLIDAY
    assert context.trading_date == holiday
