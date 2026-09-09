"""Unit tests for the historical OHLCV foundation (DECOUPLING PHASE F).

Covers bar/series invariants, trading-calendar-aware coverage + previous-trading-day, idempotent
+ conflict upsert, RAW/ADJUSTED separation and adjusted fail-closed, symbol-rename non-stitching,
source-failure fail-closed, and credential-free diagnostics. File-store restart durability and the
dynamic-universe lifecycle are in the integration suite.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.market_engine.session import TradingCalendar
from app.market_history import (
    AdjustedDataUnavailableError,
    FileHistoricalBarStore,
    HistoricalDailyBar,
    HistoricalSeries,
    HistoricalService,
    HistoryRequirement,
    InMemoryHistoricalDataSource,
    PriceAdjustment,
    UpsertOutcome,
    previous_trading_date,
    required_trading_dates,
)
from app.schemas.market_data import Instrument

_NOW = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)
# 2026-09-10 Thu, 09-11 Fri, 09-12 Sat, 09-13 Sun, 09-14 Mon
_THU, _FRI, _MON = date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 14)


def _bar(
    *,
    identity: str = "NSE:TCS",
    d: date = _THU,
    o: str = "100",
    h: str = "110",
    low: str = "95",
    c: str = "105",
    vol: int = 1000,
    adjustment: PriceAdjustment = PriceAdjustment.RAW,
) -> HistoricalDailyBar:
    return HistoricalDailyBar(
        instrument_identity=identity,
        trading_date=d,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=vol,
        adjustment=adjustment,
        source="test",
        ingested_at=_NOW,
    )


# --------------------------------------------------------------------------- #
# bar invariants + Decimal
# --------------------------------------------------------------------------- #
def test_bar_rejects_high_below_low() -> None:
    with pytest.raises(ValidationError):
        _bar(h="90", low="95")


def test_bar_rejects_open_outside_range() -> None:
    with pytest.raises(ValidationError):
        _bar(o="120", h="110", low="95")


def test_bar_rejects_non_positive_price() -> None:
    with pytest.raises(ValidationError):
        _bar(o="0")


def test_bar_rejects_negative_volume() -> None:
    with pytest.raises(ValidationError):
        _bar(vol=-1)


def test_bar_rejects_naive_ingested_at() -> None:
    with pytest.raises(ValidationError):
        HistoricalDailyBar(
            instrument_identity="NSE:TCS",
            trading_date=_THU,
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("105"),
            volume=1,
            source="test",
            ingested_at=datetime(2026, 9, 14, 6, 0),  # naive
        )


def test_bar_preserves_decimal_precision() -> None:
    bar = _bar(c="105.55")
    restored = HistoricalDailyBar.model_validate_json(bar.model_dump_json())
    assert restored.close == Decimal("105.55")
    assert isinstance(restored.close, Decimal)
    assert isinstance(restored.volume, int)


# --------------------------------------------------------------------------- #
# series
# --------------------------------------------------------------------------- #
def _series(bars: list[HistoricalDailyBar], adjustment: PriceAdjustment = PriceAdjustment.RAW):
    return HistoricalSeries(instrument_identity="NSE:TCS", adjustment=adjustment, bars=tuple(bars))


def test_series_rejects_unsorted() -> None:
    with pytest.raises(ValidationError):
        _series([_bar(d=_FRI), _bar(d=_THU)])


def test_series_rejects_duplicate_dates() -> None:
    with pytest.raises(ValidationError):
        _series([_bar(d=_THU), _bar(d=_THU)])


def test_series_rejects_wrong_instrument() -> None:
    with pytest.raises(ValidationError):
        _series([_bar(identity="NSE:INFY", d=_THU)])


def test_series_rejects_mixed_adjustment() -> None:
    with pytest.raises(ValidationError):
        _series([_bar(d=_THU, adjustment=PriceAdjustment.ADJUSTED)])  # series says RAW


def test_series_last_n_and_between() -> None:
    series = _series([_bar(d=_THU), _bar(d=_FRI), _bar(d=_MON)])
    assert series.last_n(2).trading_dates == (_FRI, _MON)
    assert series.between(_FRI, _MON).trading_dates == (_FRI, _MON)
    assert series.latest().trading_date == _MON
    assert series.previous_trading_bar(_MON).trading_date == _FRI


# --------------------------------------------------------------------------- #
# calendar-aware coverage + previous trading day
# --------------------------------------------------------------------------- #
def test_previous_trading_date_monday_resolves_friday() -> None:
    assert previous_trading_date(TradingCalendar(holidays=[]), _MON) == _FRI


def test_previous_trading_date_skips_holiday() -> None:
    calendar = TradingCalendar(holidays=[_FRI])
    assert previous_trading_date(calendar, _MON) == _THU  # Friday holiday skipped


def test_required_trading_dates_excludes_weekend() -> None:
    required = required_trading_dates(TradingCalendar(holidays=[]), as_of=_MON, trading_days=3)
    assert required == (_THU, _FRI, _MON)  # Sat/Sun not required


def test_required_trading_dates_1_20_60() -> None:
    calendar = TradingCalendar(holidays=[])
    for n in (1, 20, 60):
        required = required_trading_dates(calendar, as_of=_MON, trading_days=n)
        assert len(required) == n
        assert all(calendar.is_trading_day(d) for d in required)


def _store(tmp_path) -> FileHistoricalBarStore:
    return FileHistoricalBarStore(root=tmp_path / "history")


def _service(store: FileHistoricalBarStore, **kw) -> HistoricalService:
    return HistoricalService(
        store=store,
        source=kw.get("source", InMemoryHistoricalDataSource({})),
        calendar=kw.get("calendar", TradingCalendar(holidays=[])),
        now=lambda: _NOW,
        corporate_action_authority_available=kw.get("ca", False),
    )


def test_coverage_complete_and_incomplete(tmp_path) -> None:
    store = _store(tmp_path)
    store.upsert(_bar(d=_THU))
    store.upsert(_bar(d=_MON))
    service = _service(store)
    coverage = service.coverage("NSE:TCS", HistoryRequirement(trading_days=3), as_of=_MON)
    assert coverage.required_dates == (_THU, _FRI, _MON)
    assert coverage.missing_dates == (_FRI,)  # single hole
    assert not coverage.is_complete
    store.upsert(_bar(d=_FRI))
    assert service.coverage("NSE:TCS", HistoryRequirement(trading_days=3), as_of=_MON).is_complete


def test_coverage_empty_history(tmp_path) -> None:
    service = _service(_store(tmp_path))
    coverage = service.coverage("NSE:NEW", HistoryRequirement(trading_days=3), as_of=_MON)
    assert coverage.missing_dates == (_THU, _FRI, _MON)
    assert coverage.available_count == 0


def test_previous_trading_bar_via_service(tmp_path) -> None:
    store = _store(tmp_path)
    store.upsert(_bar(d=_FRI, c="105"))
    service = _service(store)
    bar = service.previous_trading_bar("NSE:TCS", as_of=_MON)  # Monday -> Friday
    assert bar is not None and bar.trading_date == _FRI


# --------------------------------------------------------------------------- #
# idempotency + conflict
# --------------------------------------------------------------------------- #
def test_upsert_idempotent_and_conflict(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.upsert(_bar(d=_THU, c="105")) is UpsertOutcome.INSERTED
    assert store.upsert(_bar(d=_THU, c="105")) is UpsertOutcome.UNCHANGED  # identical -> no-op
    assert store.upsert(_bar(d=_THU, c="106")) is UpsertOutcome.CONFLICT  # differing -> not applied
    stored = store.read_range("NSE:TCS", _THU, _THU, adjustment=PriceAdjustment.RAW)
    assert stored.bars[0].close == Decimal("105")  # original retained, never silently overwritten


def test_raw_and_adjusted_stored_separately(tmp_path) -> None:
    store = _store(tmp_path)
    store.upsert(_bar(d=_THU, adjustment=PriceAdjustment.RAW))
    assert store.available_dates("NSE:TCS", adjustment=PriceAdjustment.ADJUSTED) == ()
    assert store.available_dates("NSE:TCS", adjustment=PriceAdjustment.RAW) == (_THU,)


# --------------------------------------------------------------------------- #
# adjusted fail-closed + symbol rename
# --------------------------------------------------------------------------- #
def test_adjusted_request_fails_closed_without_authority(tmp_path) -> None:
    service = _service(_store(tmp_path), ca=False)
    with pytest.raises(AdjustedDataUnavailableError):
        service.coverage(
            "NSE:TCS",
            HistoryRequirement(trading_days=1),
            as_of=_MON,
            adjustment=PriceAdjustment.ADJUSTED,
        )


def test_symbol_rename_does_not_stitch_history(tmp_path) -> None:
    store = _store(tmp_path)
    store.upsert(_bar(identity="NSE:OLDNAME", d=_THU))
    service = _service(store)
    plan = service.plan_missing(
        [Instrument(exchange="NSE", symbol="NEWNAME")],
        HistoryRequirement(trading_days=1),
        as_of=_THU,
    )
    assert plan["NSE:NEWNAME"] == (_THU,)  # new identity has no history; old not stitched in
    assert store.available_dates("NSE:OLDNAME", adjustment=PriceAdjustment.RAW) == (_THU,)


# --------------------------------------------------------------------------- #
# source failure + diagnostics
# --------------------------------------------------------------------------- #
async def test_source_failure_leaves_store_untouched(tmp_path) -> None:
    source = InMemoryHistoricalDataSource({})
    source.fail_for.add("NSE:TCS")
    service = _service(_store(tmp_path), source=source)
    report = await service.backfill(
        [Instrument(exchange="NSE", symbol="TCS")], HistoryRequirement(trading_days=1), as_of=_MON
    )
    result = report.results[0]
    assert result.fetch_ok is False
    assert result.inserted == 0
    assert not result.complete_after  # no fabricated bars; coverage stays incomplete
    assert service.diagnostics().fetch_failures == 1


async def test_diagnostics_contain_no_credentials(tmp_path) -> None:
    bars = {"NSE:TCS": (_bar(d=_MON),)}
    service = _service(_store(tmp_path), source=InMemoryHistoricalDataSource(bars))
    await service.backfill(
        [Instrument(exchange="NSE", symbol="TCS")], HistoryRequirement(trading_days=1), as_of=_MON
    )
    dumped = service.diagnostics().model_dump_json().lower()
    for secret in ("token", "totp", "password", "authorization", "secret", "pin"):
        assert secret not in dumped
