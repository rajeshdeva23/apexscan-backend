"""CandleEngine authoritative repair: incomplete→finalized, idempotent, bounded (P4.5D; §42)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.market_engine.candle_engine import CandleEngine
from app.market_engine.context import MarketState, SessionContext
from app.market_engine.historical.reconciliation import ReconciliationOutcome
from app.market_engine.session import SessionSchedule
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, Instrument, Tick

_IST = ZoneInfo("Asia/Kolkata")
_TZ = "Asia/Kolkata"
_DATE = date(2026, 8, 6)
_FIVE = Timeframe.minutes(5)
_SESSION = Timeframe.session()
_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _session() -> SessionContext:
    return SessionContext(
        trading_date=_DATE, market_state=MarketState.LIVE_SESSION, exchange_timezone=_TZ
    )


def _utc(hour: int, minute: int) -> datetime:
    return datetime.combine(_DATE, time(hour, minute), tzinfo=_IST).astimezone(UTC)


def _tick(hour: int, minute: int, *, symbol: str = "RELIANCE") -> Tick:
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=datetime.combine(_DATE, time(hour, minute), tzinfo=_IST),
        last_price=Decimal("100"),
        traded_quantity=1,
        session_cumulative_volume=100,
    )


def _authoritative(
    start: datetime, end: datetime, *, symbol: str = "RELIANCE", high: str = "101"
) -> Candle:
    return Candle(
        instrument=_instrument(symbol),
        start_timestamp=start,
        end_timestamp=end,
        open_price=Decimal("100"),
        high_price=Decimal(high),
        low_price=Decimal("99"),
        close_price=Decimal("100"),
        traded_quantity=500,
    )


def _engine(timeframes: list[Timeframe]) -> CandleEngine:
    return CandleEngine(schedule=_SCHEDULE, exchange_timezone=_TZ, timeframes=timeframes)


def _candles(engine: CandleEngine, timeframe: Timeframe, symbol: str = "RELIANCE"):  # noqa: ANN202
    return next(c for c in engine.candle_sets_for(_instrument(symbol)) if c.timeframe == timeframe)


def _engine_with_incomplete_5m() -> CandleEngine:
    engine = _engine([_FIVE])
    engine.update(_tick(9, 16), _session())
    engine.flush(_utc(9, 20))  # finalize 09:15-09:20 as incomplete
    return engine


def test_successful_repair_moves_incomplete_to_finalized() -> None:
    engine = _engine_with_incomplete_5m()
    result = engine.reconcile(_authoritative(_utc(9, 15), _utc(9, 20)), _FIVE)
    assert result.outcome is ReconciliationOutcome.RECONCILED
    candles = _candles(engine, _FIVE)
    assert len(candles.finalized) == 1
    assert candles.finalized[0].high_price == Decimal("101")
    assert candles.incomplete == ()


def test_repair_is_idempotent() -> None:
    engine = _engine_with_incomplete_5m()
    authoritative = _authoritative(_utc(9, 15), _utc(9, 20))
    engine.reconcile(authoritative, _FIVE)
    second = engine.reconcile(authoritative, _FIVE)
    assert second.outcome is ReconciliationOutcome.ALREADY_RECONCILED
    assert len(_candles(engine, _FIVE).finalized) == 1


def test_conflicting_authoritative_does_not_overwrite() -> None:
    engine = _engine_with_incomplete_5m()
    engine.reconcile(_authoritative(_utc(9, 15), _utc(9, 20), high="101"), _FIVE)
    conflict = engine.reconcile(_authoritative(_utc(9, 15), _utc(9, 20), high="106"), _FIVE)
    assert conflict.outcome is ReconciliationOutcome.CONFLICT
    assert _candles(engine, _FIVE).finalized[0].high_price == Decimal("101")  # unchanged


def test_disagreeing_authoritative_ohlc_still_finalizes_from_authority() -> None:
    engine = _engine_with_incomplete_5m()  # live incomplete high defaults from tick price
    result = engine.reconcile(_authoritative(_utc(9, 15), _utc(9, 20), high="106"), _FIVE)
    assert result.outcome is ReconciliationOutcome.RECONCILED
    assert _candles(engine, _FIVE).finalized[0].high_price == Decimal("106")


def test_active_partial_is_untouched() -> None:
    engine = _engine([_FIVE])
    engine.update(_tick(9, 16), _session())  # 09:15-09:20 partial
    engine.update(_tick(9, 21), _session())  # finalize 09:15-09:20, open 09:20-09:25 partial
    engine.reconcile(_authoritative(_utc(9, 15), _utc(9, 20)), _FIVE)
    partial = _candles(engine, _FIVE).partial
    assert partial is not None
    assert partial.start_timestamp == _utc(9, 20)
    assert partial.end_timestamp == _utc(9, 25)


def test_out_of_window_candle_is_not_resurrected() -> None:
    engine = _engine([_FIVE])
    engine.update(_tick(9, 16), _session())
    engine.update(_tick(9, 21), _session())  # incomplete 09:15-09:20 + partial 09:20-09:25
    result = engine.reconcile(_authoritative(_utc(9, 0), _utc(9, 5)), _FIVE)  # predates retained
    assert result.outcome is ReconciliationOutcome.OUT_OF_WINDOW
    assert all(c.start_timestamp >= _utc(9, 15) for c in _candles(engine, _FIVE).finalized)


def test_no_matching_incomplete_is_no_match() -> None:
    engine = _engine_with_incomplete_5m()
    result = engine.reconcile(_authoritative(_utc(9, 20), _utc(9, 25)), _FIVE)
    assert result.outcome is ReconciliationOutcome.NO_MATCH
    assert _candles(engine, _FIVE).finalized == ()  # not injected


def test_repair_does_not_affect_another_instrument() -> None:
    engine = _engine([_FIVE])
    for symbol in ("RELIANCE", "TCS"):
        engine.update(_tick(9, 16, symbol=symbol), _session())
    engine.flush(_utc(9, 20))
    engine.reconcile(_authoritative(_utc(9, 15), _utc(9, 20), symbol="RELIANCE"), _FIVE)
    assert len(_candles(engine, _FIVE, "RELIANCE").finalized) == 1
    assert _candles(engine, _FIVE, "TCS").finalized == ()
    assert len(_candles(engine, _FIVE, "TCS").incomplete) == 1


def test_session_timeframe_repair_with_exact_identity() -> None:
    engine = _engine([_SESSION])
    engine.update(_tick(9, 16), _session())
    engine.flush(_utc(15, 30))  # finalize the whole-session incomplete 09:15-15:30
    result = engine.reconcile(_authoritative(_utc(9, 15), _utc(15, 30)), _SESSION)
    assert result.outcome is ReconciliationOutcome.RECONCILED
    assert len(_candles(engine, _SESSION).finalized) == 1
