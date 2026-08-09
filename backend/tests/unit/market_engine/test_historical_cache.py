"""Exact-coverage historical cache behaviour (P4.5B; §41)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.market_engine.historical.cache import HistoricalCache
from app.market_engine.historical.source import HistoricalRequestKey
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, Instrument

_INSTRUMENT = Instrument(exchange="NSE", symbol="RELIANCE")
_FIVE = Timeframe.minutes(5)
_FIFTEEN = Timeframe.minutes(15)
_BASE = datetime(2026, 8, 7, 3, 45, tzinfo=UTC)


def _candle(minute: int) -> Candle:
    start = _BASE + timedelta(minutes=minute)
    return Candle(
        instrument=_INSTRUMENT,
        start_timestamp=start,
        end_timestamp=start + timedelta(minutes=5),
        open_price=Decimal("100"),
        high_price=Decimal("101"),
        low_price=Decimal("99"),
        close_price=Decimal("100"),
        traded_quantity=10,
    )


def _key(start_min: int, end_min: int, *, timeframe: Timeframe = _FIVE) -> HistoricalRequestKey:
    return HistoricalRequestKey(
        instrument=_INSTRUMENT,
        timeframe=timeframe,
        start=_BASE + timedelta(minutes=start_min),
        end=_BASE + timedelta(minutes=end_min),
    )


def _candles(start_min: int, end_min: int) -> tuple[Candle, ...]:
    return tuple(_candle(minute) for minute in range(start_min, end_min, 5))


def test_full_coverage_hit_returns_all_candles() -> None:
    cache = HistoricalCache()
    cache.put(_key(0, 60), _candles(0, 60))
    assert cache.get(_key(0, 60)) == _candles(0, 60)


def test_subrange_hit_returns_only_the_subrange() -> None:
    cache = HistoricalCache()
    cache.put(_key(0, 60), _candles(0, 60))
    assert cache.get(_key(10, 30)) == _candles(10, 30)


def test_partial_coverage_is_a_miss() -> None:
    cache = HistoricalCache()
    cache.put(_key(0, 60), _candles(0, 60))
    assert cache.get(_key(30, 90)) is None


def test_outside_coverage_is_a_miss() -> None:
    cache = HistoricalCache()
    cache.put(_key(0, 60), _candles(0, 60))
    assert cache.get(_key(120, 180)) is None


def test_missing_entry_is_a_miss() -> None:
    assert HistoricalCache().get(_key(0, 60)) is None


def test_return_is_an_immutable_tuple() -> None:
    cache = HistoricalCache()
    cache.put(_key(0, 60), _candles(0, 60))
    result = cache.get(_key(0, 60))
    assert isinstance(result, tuple)


def test_larger_window_serves_a_smaller_lookback() -> None:
    cache = HistoricalCache()
    cache.put(_key(0, 120), _candles(0, 120))
    assert cache.get(_key(60, 120)) == _candles(60, 120)


def test_retain_timeframes_evicts_inactive_entries() -> None:
    cache = HistoricalCache()
    cache.put(_key(0, 60), _candles(0, 60))
    cache.put(_key(0, 60, timeframe=_FIFTEEN), _candles(0, 60))
    assert cache.entry_count() == 2
    cache.retain_timeframes(frozenset({_FIVE}))
    assert cache.entry_count() == 1
    assert cache.get(_key(0, 60, timeframe=_FIFTEEN)) is None
    assert cache.get(_key(0, 60)) is not None
