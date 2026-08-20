"""Multi-interval special sessions: disjoint live blocks with a closed gap.

Covers the ADR-011 multi-interval addendum (MI1–MI20): the canonical
``TradingInterval``; ``TradingSessionOverride`` interval validation; the
``EffectiveSchedule.intervals_for``/``envelope_for`` lookups; per-interval bucket
generation with a contiguous global index that never spans the closed gap;
per-date capacity as the sum of interval capacities (21, never the 39 the envelope
would give); planner lookback resolution over a multi-block date; H3 fail-closed vs.
resolvable intraday; independent per-interval reconstruction with the gap ignored;
and session OHLC aggregating naturally across both blocks. All dates are synthetic;
no real NSE/Muhurat/BCP dates are used.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.market_engine.buckets import day_buckets, session_buckets
from app.market_engine.historical.cache import HistoricalCache
from app.market_engine.historical.calendar_window import (
    CalendarCoverage,
    HistoricalCalendarWindow,
    MissingSessionTimingError,
)
from app.market_engine.historical.context import HistoricalSeries
from app.market_engine.historical.coordinator import HistoricalCoordinator
from app.market_engine.historical.requirements import HistoricalRequirement
from app.market_engine.historical.resampling import reconstruct_series
from app.market_engine.historical.service import HistoricalRangePlanner, HistoricalWarmupService
from app.market_engine.session import (
    EffectiveSchedule,
    SessionSchedule,
    TradingCalendar,
    TradingInterval,
    TradingSessionOverride,
)
from app.market_engine.timeframe import Timeframe
from app.schemas.market_data import Candle, Instrument

_IST = ZoneInfo("Asia/Kolkata")
_TZ = "Asia/Kolkata"
_SATURDAY = date(2026, 8, 8)  # synthetic exceptional multi-block OPEN session
_THURSDAY = date(2026, 8, 6)
_FRIDAY = date(2026, 8, 7)
_MONDAY_REF = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)  # 11:30 IST Monday
_ONE = Timeframe.minutes(1)
_FIVE = Timeframe.minutes(5)
_SESSION = Timeframe.session()

_BLOCK1 = TradingInterval(start=time(9, 15), end=time(10, 0))  # 45m -> 9 @5m
_BLOCK2 = TradingInterval(start=time(11, 30), end=time(12, 30))  # 60m -> 12 @5m
_INTERVALS = (_BLOCK1, _BLOCK2)

_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)


def _multi_override() -> TradingSessionOverride:
    return TradingSessionOverride(trading_date=_SATURDAY, live_intervals=_INTERVALS)


def _ist(day: date, moment: time) -> datetime:
    return datetime.combine(day, moment, tzinfo=_IST).astimezone(UTC)


# --------------------------------------------------------------------------- #
# (1) TradingSessionOverride interval validation (MI3–MI6)
# --------------------------------------------------------------------------- #
def test_single_interval_accepted() -> None:
    override = TradingSessionOverride(trading_date=_SATURDAY, live_intervals=(_BLOCK1,))
    assert override.live_intervals == (_BLOCK1,)


def test_multiple_intervals_accepted() -> None:
    assert _multi_override().live_intervals == _INTERVALS


def test_empty_intervals_rejected() -> None:
    with pytest.raises(ValueError, match="at least one live interval"):
        TradingSessionOverride(trading_date=_SATURDAY, live_intervals=())


def test_interval_start_equal_end_rejected() -> None:
    with pytest.raises(ValueError, match="start < end"):
        TradingInterval(start=time(10, 0), end=time(10, 0))


def test_interval_start_after_end_rejected() -> None:
    with pytest.raises(ValueError, match="start < end"):
        TradingInterval(start=time(11, 0), end=time(10, 0))


def test_unordered_intervals_rejected() -> None:
    with pytest.raises(ValueError, match="chronologically ordered"):
        TradingSessionOverride(trading_date=_SATURDAY, live_intervals=(_BLOCK2, _BLOCK1))


def test_duplicate_intervals_rejected() -> None:
    with pytest.raises(ValueError, match="chronologically ordered"):
        TradingSessionOverride(trading_date=_SATURDAY, live_intervals=(_BLOCK1, _BLOCK1))


def test_overlapping_intervals_rejected() -> None:
    overlap = TradingInterval(start=time(9, 45), end=time(10, 30))
    with pytest.raises(ValueError, match="chronologically ordered"):
        TradingSessionOverride(trading_date=_SATURDAY, live_intervals=(_BLOCK1, overlap))


def test_touching_intervals_rejected() -> None:
    touching = TradingInterval(start=time(10, 0), end=time(11, 0))  # starts at BLOCK1.end
    with pytest.raises(ValueError, match="strictly positive gap"):
        TradingSessionOverride(trading_date=_SATURDAY, live_intervals=(_BLOCK1, touching))


# --------------------------------------------------------------------------- #
# (2) EffectiveSchedule.intervals_for / envelope_for (MI16 / MI10)
# --------------------------------------------------------------------------- #
def test_intervals_for_ordinary_date_is_default() -> None:
    effective = EffectiveSchedule(default=_SCHEDULE)
    assert effective.intervals_for(_THURSDAY) == (_SCHEDULE.bounds,)


def test_intervals_for_single_block_special_date() -> None:
    override = TradingSessionOverride.continuous(
        trading_date=_SATURDAY, start=time(10, 0), end=time(14, 0)
    )
    effective = EffectiveSchedule(default=_SCHEDULE, overrides=(override,))
    assert effective.intervals_for(_SATURDAY) == (
        TradingInterval(start=time(10, 0), end=time(14, 0)),
    )


def test_intervals_for_multi_block_in_order_with_no_leakage() -> None:
    effective = EffectiveSchedule(default=_SCHEDULE, overrides=(_multi_override(),))
    assert effective.intervals_for(_SATURDAY) == _INTERVALS
    assert effective.intervals_for(_FRIDAY) == (_SCHEDULE.bounds,)  # adjacent ordinary


def test_envelope_for_multi_block_spans_first_start_to_last_end() -> None:
    effective = EffectiveSchedule(default=_SCHEDULE, overrides=(_multi_override(),))
    assert effective.envelope_for(_SATURDAY) == TradingInterval(start=time(9, 15), end=time(12, 30))
    assert effective.envelope_for(_THURSDAY) == _SCHEDULE.bounds


# --------------------------------------------------------------------------- #
# (3) Multi-interval buckets: 21 buckets, contiguous index, gap never spanned
# --------------------------------------------------------------------------- #
def _bounds(bucket: tuple[int, datetime, datetime]) -> tuple[datetime, datetime]:
    return bucket[1], bucket[2]


def test_day_buckets_multi_interval_shape() -> None:
    buckets = day_buckets(
        trading_date=_SATURDAY, timeframe=_FIVE, intervals=_INTERVALS, timezone=_IST
    )
    assert len(buckets) == 21
    assert [index for index, _s, _e in buckets] == list(range(21))  # contiguous global index
    assert _bounds(buckets[0]) == (_ist(_SATURDAY, time(9, 15)), _ist(_SATURDAY, time(9, 20)))
    assert _bounds(buckets[8]) == (_ist(_SATURDAY, time(9, 55)), _ist(_SATURDAY, time(10, 0)))
    assert _bounds(buckets[9]) == (_ist(_SATURDAY, time(11, 30)), _ist(_SATURDAY, time(11, 35)))
    assert _bounds(buckets[20]) == (_ist(_SATURDAY, time(12, 25)), _ist(_SATURDAY, time(12, 30)))


def test_day_buckets_never_span_or_enter_the_closed_gap() -> None:
    gap_start = _ist(_SATURDAY, time(10, 0))
    gap_end = _ist(_SATURDAY, time(11, 30))
    buckets = day_buckets(
        trading_date=_SATURDAY, timeframe=_FIVE, intervals=_INTERVALS, timezone=_IST
    )
    for _index, start, end in buckets:
        assert end <= gap_start or start >= gap_end  # wholly in a block, never in the gap


def test_single_interval_day_buckets_is_byte_identical_to_session_buckets() -> None:
    day = day_buckets(
        trading_date=_THURSDAY, timeframe=_FIVE, intervals=(_SCHEDULE.bounds,), timezone=_IST
    )
    session = session_buckets(
        trading_date=_THURSDAY, timeframe=_FIVE, interval=_SCHEDULE.bounds, timezone=_IST
    )
    assert day == session


# --------------------------------------------------------------------------- #
# (4) Capacity: 21, never the envelope's 39
# --------------------------------------------------------------------------- #
def test_multi_interval_capacity_is_21_not_39() -> None:
    live = day_buckets(trading_date=_SATURDAY, timeframe=_FIVE, intervals=_INTERVALS, timezone=_IST)
    envelope = day_buckets(
        trading_date=_SATURDAY,
        timeframe=_FIVE,
        intervals=(TradingInterval(start=time(9, 15), end=time(12, 30)),),
        timezone=_IST,
    )
    assert len(live) == 21
    assert len(envelope) == 39
    assert len(live) != len(envelope)  # the gap contributes zero capacity


# --------------------------------------------------------------------------- #
# (5) Planner lookback resolution across ordinary / multi-block / ordinary dates
# --------------------------------------------------------------------------- #
def _planner(*, overrides: tuple[TradingSessionOverride, ...]) -> HistoricalRangePlanner:
    window = HistoricalCalendarWindow(
        calendar=TradingCalendar(open_sessions=(_SATURDAY,)),
        coverage=CalendarCoverage(start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)),
    )
    return HistoricalRangePlanner(
        schedule=_SCHEDULE, exchange_timezone=_TZ, calendar_window=window, overrides=overrides
    )


def test_planner_lookback_satisfied_partway_through_multi_block_capacity() -> None:
    planner = _planner(overrides=(_multi_override(),))
    # lookback 5 << Saturday capacity 21: newest date is the multi-block Saturday.
    start, end = planner.resolve(HistoricalRequirement(timeframe=_FIVE, lookback=5), _MONDAY_REF)
    assert end == _ist(_SATURDAY, time(12, 30))  # last interval end (envelope end)
    assert start == _ist(_FRIDAY, time(9, 15))  # +1 margin session, default open


def test_planner_capacity_forces_extra_date_proving_21_not_39() -> None:
    planner = _planner(overrides=(_multi_override(),))
    # lookback 30 exceeds capacity 21 (would fit inside the envelope's 39), so the
    # walk must reach an extra ordinary day. Envelope capacity would stop one date short.
    start, end = planner.resolve(HistoricalRequirement(timeframe=_FIVE, lookback=30), _MONDAY_REF)
    assert start == _ist(_THURSDAY, time(9, 15))  # reaches Thursday: proves 21, not 39
    assert end == _ist(_SATURDAY, time(12, 30))


# --------------------------------------------------------------------------- #
# (6) H3: resolvable with complete intervals; fail-closed without; session ok (M17)
# --------------------------------------------------------------------------- #
def test_h3_multi_block_intraday_resolves_with_interval_boundaries() -> None:
    planner = _planner(overrides=(_multi_override(),))
    _start, end = planner.resolve(HistoricalRequirement(timeframe=_FIVE, lookback=5), _MONDAY_REF)
    assert end == _ist(_SATURDAY, time(12, 30))  # boundary from the last interval, not 15:30


def test_h3_open_without_timing_intraday_fails_closed() -> None:
    planner = _planner(overrides=())
    with pytest.raises(MissingSessionTimingError, match="session-hours"):
        planner.resolve(HistoricalRequirement(timeframe=_FIVE, lookback=5), _MONDAY_REF)


def test_h3_session_timeframe_resolves_over_special_date() -> None:
    planner = _planner(overrides=())  # no timing, but session/daily never fails (M17)
    start, end = planner.resolve(HistoricalRequirement(timeframe=_SESSION, lookback=2), _MONDAY_REF)
    assert end == _ist(_SATURDAY, time(15, 30))  # default close for the fetch window
    assert start == _ist(_FRIDAY, time(9, 15))


# --------------------------------------------------------------------------- #
# (7) Reconstruction: both blocks rebuilt, gap ignored, completeness gating
# --------------------------------------------------------------------------- #
def _instrument() -> Instrument:
    return Instrument(exchange="NSE", symbol="RELIANCE")


def _minute(
    anchor: time,
    offset: int,
    *,
    high: str = "101",
    low: str = "99",
    open_price: str = "100",
    close: str = "100",
    day: date = _SATURDAY,
) -> Candle:
    start = (datetime.combine(day, anchor, tzinfo=_IST) + timedelta(minutes=offset)).astimezone(UTC)
    return Candle(
        instrument=_instrument(),
        start_timestamp=start,
        end_timestamp=start + timedelta(minutes=1),
        open_price=Decimal(open_price),
        high_price=Decimal(high),
        low_price=Decimal(low),
        close_price=Decimal(close),
        traded_quantity=10,
    )


def _reconstruct(candles: tuple[Candle, ...]) -> HistoricalSeries | None:
    return reconstruct_series(
        source=HistoricalSeries(timeframe=_ONE, candles=candles),
        target=_FIVE,
        effective=EffectiveSchedule(default=_SCHEDULE, overrides=(_multi_override(),)),
        calendar=TradingCalendar(open_sessions=(_SATURDAY,)),
        exchange_timezone=_TZ,
    )


def test_reconstruction_rebuilds_both_blocks_and_ignores_gap() -> None:
    block1 = tuple(_minute(time(9, 15), off) for off in range(45))
    block2 = tuple(_minute(time(11, 30), off) for off in range(60))
    gap = (_minute(time(10, 30), 0), _minute(time(11, 0), 0))  # inside the closed gap
    result = _reconstruct(block1 + gap + block2)
    assert result is not None
    assert len(result.candles) == 21  # 9 + 12, gap yields none
    assert result.candles[0].start_timestamp == _ist(_SATURDAY, time(9, 15))
    assert result.candles[8].end_timestamp == _ist(_SATURDAY, time(10, 0))
    assert result.candles[9].start_timestamp == _ist(_SATURDAY, time(11, 30))
    assert result.candles[-1].end_timestamp == _ist(_SATURDAY, time(12, 30))
    gap_start, gap_end = _ist(_SATURDAY, time(10, 0)), _ist(_SATURDAY, time(11, 30))
    for candle in result.candles:  # no synthetic candle bridges the gap
        assert candle.end_timestamp <= gap_start or candle.start_timestamp >= gap_end


def test_reconstruction_missing_one_real_bucket_is_incomplete() -> None:
    block1 = tuple(_minute(time(9, 15), off) for off in range(45) if off != 3)  # drop 09:18-09:19
    block2 = tuple(_minute(time(11, 30), off) for off in range(60))
    result = _reconstruct(block1 + block2)
    assert result is not None
    assert len(result.candles) == 20  # the 09:15-09:20 bucket is withheld
    assert result.candles[0].start_timestamp == _ist(_SATURDAY, time(9, 20))


# --------------------------------------------------------------------------- #
# (8) Session OHLC aggregates naturally across both blocks (MI10)
# --------------------------------------------------------------------------- #
def test_session_ohlc_across_two_blocks_uses_ordinary_aggregation() -> None:
    block1 = [_minute(time(9, 15), off) for off in range(45)]
    block2 = [_minute(time(11, 30), off) for off in range(60)]
    block1[0] = _minute(time(9, 15), 0, open_price="100")  # first block first open
    block1[20] = _minute(time(9, 15), 20, low="90")  # global low in block 1
    block2[5] = _minute(time(11, 30), 5, high="120")  # global high in block 2
    block2[59] = _minute(time(11, 30), 59, high="108", close="108")  # last block last close
    result = _reconstruct(tuple(block1) + tuple(block2))
    assert result is not None
    candles = result.candles
    assert candles[0].open_price == Decimal("100")  # OPEN = first interval's first
    assert candles[-1].close_price == Decimal("108")  # CLOSE = last interval's last
    assert max(c.high_price for c in candles) == Decimal("120")  # HIGH = max over both
    assert min(c.low_price for c in candles) == Decimal("90")  # LOW = min over both


# --------------------------------------------------------------------------- #
# (9) Ordinary regression + current-day isolation
# --------------------------------------------------------------------------- #
def test_ordinary_reconstruction_unchanged_by_multi_interval_support() -> None:
    source = tuple(_minute(time(9, 15), off, day=_THURSDAY) for off in range(10))
    result = reconstruct_series(
        source=HistoricalSeries(timeframe=_ONE, candles=source),
        target=_FIVE,
        effective=EffectiveSchedule(default=_SCHEDULE),  # no overrides -> ordinary path
        calendar=TradingCalendar(),  # Thursday is an ordinary trading day
        exchange_timezone=_TZ,
    )
    assert result is not None
    assert len(result.candles) == 2  # 09:15-09:20, 09:20-09:25


def test_warmup_service_current_day_isolation_default_false() -> None:
    instrument = _instrument()
    from app.market_engine.state import InstrumentStateRegistry
    from tests.fakes.historical_source import FakeHistoricalSource

    coordinator = HistoricalCoordinator(
        source=FakeHistoricalSource(direct_timeframes=frozenset({_FIVE})),
        cache=HistoricalCache(),
        max_concurrency=4,
    )
    service = HistoricalWarmupService(
        registry=InstrumentStateRegistry([instrument]),
        coordinator=coordinator,
        planner=_planner(overrides=(_multi_override(),)),
    )
    assert service._supports_current_day is False  # noqa: SLF001 - isolation invariant
