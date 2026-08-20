"""Daily→session canonicalization and end-to-end session reconciliation (P4.5E; §7-12,34,35)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.market_engine.candle_engine import CandleEngine
from app.market_engine.context import MarketState, SessionContext
from app.market_engine.historical.cache import HistoricalCache
from app.market_engine.historical.calendar_window import CalendarCoverage, HistoricalCalendarWindow
from app.market_engine.historical.coordinator import HistoricalCoordinator
from app.market_engine.historical.reconciliation import ReconciliationOutcome
from app.market_engine.historical.service import HistoricalRangePlanner, HistoricalWarmupService
from app.market_engine.historical.session_candles import (
    canonical_session_series,
    canonicalize_session_candle,
)
from app.market_engine.session import EffectiveSchedule, SessionSchedule, TradingCalendar
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, Instrument, Tick
from tests.fakes.historical_source import FakeHistoricalSource

_IST = ZoneInfo("Asia/Kolkata")
_TZ = "Asia/Kolkata"
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


def _daily(day: date, *, hour: int = 12) -> Candle:
    """A provider daily bar with an arbitrary within-day start and +1-day end."""
    start = datetime.combine(day, time(hour, 0), tzinfo=_IST).astimezone(UTC)
    return Candle(
        instrument=_instrument(),
        start_timestamp=start,
        end_timestamp=start + timedelta(days=1),
        open_price=Decimal("100"),
        high_price=Decimal("110"),
        low_price=Decimal("95"),
        close_price=Decimal("108"),
        traded_quantity=999,
    )


def _canonical(candle: Candle, *, calendar: TradingCalendar | None = None) -> Candle | None:
    return canonicalize_session_candle(
        candle,
        effective=EffectiveSchedule(default=_SCHEDULE),
        calendar=calendar or TradingCalendar(),
        exchange_timezone=_TZ,
    )


def test_canonicalizes_to_regular_session_bounds() -> None:
    canon = _canonical(_daily(date(2026, 8, 6)))
    assert canon is not None
    assert canon.start_timestamp == datetime(2026, 8, 6, 9, 15, tzinfo=_IST).astimezone(UTC)
    assert canon.end_timestamp == datetime(2026, 8, 6, 15, 30, tzinfo=_IST).astimezone(UTC)
    assert canon.open_price == Decimal("100")  # OHLCV preserved
    assert canon.traded_quantity == 999


def test_holiday_daily_bar_is_withheld() -> None:
    calendar = TradingCalendar(holidays=(date(2026, 8, 6),))
    assert _canonical(_daily(date(2026, 8, 6)), calendar=calendar) is None


def test_canonical_bounds_for_weekday_friday_and_monday() -> None:
    for day in (date(2026, 8, 5), date(2026, 8, 7), date(2026, 8, 10)):  # Wed, Fri, Mon
        canon = _canonical(_daily(day))
        assert canon is not None
        open_utc = datetime.combine(day, time(9, 15), tzinfo=_IST).astimezone(UTC)
        close_utc = datetime.combine(day, time(15, 30), tzinfo=_IST).astimezone(UTC)
        assert canon.start_timestamp == open_utc
        assert canon.end_timestamp == close_utc


def test_series_drops_non_trading_days_and_dedups() -> None:
    bars = (
        _daily(date(2026, 8, 6)),  # Thu
        _daily(date(2026, 8, 7)),  # Fri
        _daily(date(2026, 8, 8)),  # Sat (weekend) — dropped
        _daily(date(2026, 8, 6)),  # duplicate trading date — collapsed
    )
    series = canonical_session_series(
        bars,
        effective=EffectiveSchedule(default=_SCHEDULE),
        calendar=TradingCalendar(),
        exchange_timezone=_TZ,
    )
    dates = {c.start_timestamp.astimezone(_IST).date() for c in series}
    assert dates == {date(2026, 8, 6), date(2026, 8, 7)}
    assert len(series) == 2


def test_canonicalization_is_timezone_generic_across_dst() -> None:
    # America/New_York shifts UTC offset across DST; canonical bounds must follow ZoneInfo.
    ny = "America/New_York"
    schedule = SessionSchedule(
        pre_open_start=time(9, 0),
        opening_auction_start=time(9, 15),
        regular_open=time(9, 30),
        regular_close=time(16, 0),
        closing_end=time(16, 15),
    )
    tzinfo = ZoneInfo(ny)

    def canon_open(day: date) -> datetime:
        bar = Candle(
            instrument=_instrument(),
            start_timestamp=datetime.combine(day, time(12, 0), tzinfo=tzinfo).astimezone(UTC),
            end_timestamp=datetime.combine(day, time(12, 0), tzinfo=tzinfo).astimezone(UTC)
            + timedelta(days=1),
            open_price=Decimal("1"),
            high_price=Decimal("1"),
            low_price=Decimal("1"),
            close_price=Decimal("1"),
            traded_quantity=1,
        )
        result = canonicalize_session_candle(
            bar,
            effective=EffectiveSchedule(default=schedule),
            calendar=TradingCalendar(),
            exchange_timezone=ny,
        )
        assert result is not None
        return result.start_timestamp

    winter = canon_open(date(2026, 1, 15)).timetz()  # EST (UTC-5) → 14:30Z
    summer = canon_open(date(2026, 7, 15)).timetz()  # EDT (UTC-4) → 13:30Z
    assert winter != summer  # offset follows DST, not a fixed offset


# --------------------------------------------------------------------------- #
# End-to-end session reconciliation (§12)
# --------------------------------------------------------------------------- #
async def test_prior_session_incomplete_reconciles_after_canonicalization() -> None:
    prior = date(2026, 8, 6)
    reference = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)
    instrument = _instrument()
    engine = CandleEngine(schedule=_SCHEDULE, exchange_timezone=_TZ, timeframes=[_SESSION])
    session = SessionContext(
        trading_date=prior, market_state=MarketState.LIVE_SESSION, exchange_timezone=_TZ
    )
    engine.update(
        Tick(
            instrument=instrument,
            event_timestamp=datetime.combine(prior, time(9, 16), tzinfo=_IST),
            last_price=Decimal("100"),
            traded_quantity=1,
            session_cumulative_volume=100,
        ),
        session,
    )
    engine.flush(datetime.combine(prior, time(15, 30), tzinfo=_IST).astimezone(UTC))

    window = HistoricalCalendarWindow(
        calendar=TradingCalendar(),
        coverage=CalendarCoverage(start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)),
    )
    planner = HistoricalRangePlanner(
        schedule=_SCHEDULE, exchange_timezone=_TZ, calendar_window=window
    )
    source = FakeHistoricalSource(direct_timeframes=frozenset({_SESSION}))
    coordinator = HistoricalCoordinator(source=source, cache=HistoricalCache(), max_concurrency=4)
    service = HistoricalWarmupService(
        registry=InstrumentStateRegistry([instrument]),
        coordinator=coordinator,
        planner=planner,
        candles=engine,
    )

    summary = await service.reconcile_completed(instrument, reference=reference)
    assert ReconciliationOutcome.RECONCILED in [result.outcome for result in summary.results]
    finalized = next(
        c for c in engine.candle_sets_for(instrument) if c.timeframe == _SESSION
    ).finalized
    assert len(finalized) == 1
    assert finalized[0].start_timestamp == datetime.combine(
        prior, time(9, 15), tzinfo=_IST
    ).astimezone(UTC)
    assert finalized[0].end_timestamp == datetime.combine(
        prior, time(15, 30), tzinfo=_IST
    ).astimezone(UTC)
