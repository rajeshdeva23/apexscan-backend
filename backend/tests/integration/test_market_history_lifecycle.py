"""Historical durability + dynamic-universe lifecycle integration (DECOUPLING PHASE F).

Uses a disposable local filesystem (tmp_path) — never a production DB/Redis/Dhan. Drives the
historical service with the instruments of real Phase-E UniverseSnapshots across add/remove/
re-add, proves restart durability and exact data round-trip, and exercises 20- and 60-day
requirements.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from app.market_engine.session import TradingCalendar
from app.market_history import (
    FileHistoricalBarStore,
    HistoricalDailyBar,
    HistoricalService,
    HistoryRequirement,
    InMemoryHistoricalDataSource,
    PriceAdjustment,
    UpsertOutcome,
    required_trading_dates,
)
from app.market_universe import (
    InMemoryProviderMappingSource,
    ProviderMapping,
    SourceProvenance,
    UniverseResolver,
    UniverseSnapshot,
)
from app.schemas.market_data import Instrument

_NOW = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)
_AS_OF = date(2026, 9, 14)  # Monday
_CALENDAR = TradingCalendar(holidays=[])


class _Sector:
    def resolve_primary(self, identity: str, on: date | None = None) -> str | None:
        return "IT"


def _instrument(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _snapshot(symbols: list[str]) -> UniverseSnapshot:
    mappings = {
        _instrument(s): ProviderMapping(provider_security_id=s, exchange_segment="NSE_FNO")
        for s in symbols
    }
    resolver = UniverseResolver(
        mapping_source=InMemoryProviderMappingSource(mappings), sector_authority=_Sector()
    )
    provenance = SourceProvenance(
        fno_source="nse",
        fno_version="v",
        fno_effective_date=_AS_OF,
        instrument_master_source="d",
        instrument_master_version="v",
        sector_dataset_id="s",
        sector_version="1",
    )
    return resolver.resolve(
        [_instrument(s) for s in symbols],
        trading_date=_AS_OF,
        effective_at=_NOW,
        provenance=provenance,
    ).candidate


def _instruments_of(snapshot: UniverseSnapshot) -> list[Instrument]:
    return [item.instrument for item in snapshot.instruments]


def _seed_source(symbols: list[str], dates: tuple[date, ...]) -> InMemoryHistoricalDataSource:
    bars: dict[str, tuple[HistoricalDailyBar, ...]] = {}
    for symbol in symbols:
        bars[f"NSE:{symbol}"] = tuple(
            HistoricalDailyBar(
                instrument_identity=f"NSE:{symbol}",
                trading_date=d,
                open=Decimal("100.10"),
                high=Decimal("110.20"),
                low=Decimal("95.30"),
                close=Decimal("105.40"),
                volume=1_000 + i,
                source="fixture",
                ingested_at=_NOW,
            )
            for i, d in enumerate(dates)
        )
    return InMemoryHistoricalDataSource(bars)


def _service(
    store: FileHistoricalBarStore, source: InMemoryHistoricalDataSource
) -> HistoricalService:
    return HistoricalService(store=store, source=source, calendar=_CALENDAR, now=lambda: _NOW)


async def test_dynamic_universe_lifecycle_add_remove_readd(tmp_path: Path) -> None:
    requirement = HistoryRequirement(trading_days=20)
    dates = required_trading_dates(_CALENDAR, as_of=_AS_OF, trading_days=20)
    store = FileHistoricalBarStore(root=tmp_path / "history")
    source = _seed_source(["A", "B", "C", "D"], dates)
    service = _service(store, source)

    # V1 {A,B,C}: backfill all three complete
    v1 = await service.backfill(
        _instruments_of(_snapshot(["A", "B", "C"])), requirement, as_of=_AS_OF
    )
    assert all(r.complete_after for r in v1.results)
    assert store.available_dates("NSE:D", adjustment=PriceAdjustment.RAW) == ()  # D untouched

    # V2 {A,B,C,D}: D backfilled; A,B,C idempotent (already complete)
    v2 = await service.backfill(
        _instruments_of(_snapshot(["A", "B", "C", "D"])), requirement, as_of=_AS_OF
    )
    by_id = {r.instrument_identity: r for r in v2.results}
    assert by_id["NSE:D"].inserted == 20 and by_id["NSE:D"].complete_after
    assert by_id["NSE:A"].inserted == 0  # already present -> nothing re-fetched/inserted

    # V3 {A,B,C}: D removed from universe -> its history is retained, not deleted
    await service.backfill(_instruments_of(_snapshot(["A", "B", "C"])), requirement, as_of=_AS_OF)
    assert len(store.available_dates("NSE:D", adjustment=PriceAdjustment.RAW)) == 20  # retained

    # V4 {A,B,C,D}: D reuses existing history; nothing missing, only unchanged
    v4 = await service.backfill(
        _instruments_of(_snapshot(["A", "B", "C", "D"])), requirement, as_of=_AS_OF
    )
    d_result = {r.instrument_identity: r for r in v4.results}["NSE:D"]
    assert d_result.missing_before == ()  # incremental: no re-download of full history
    assert d_result.inserted == 0 and d_result.complete_after


async def test_incremental_backfill_of_single_missing_day(tmp_path: Path) -> None:
    requirement = HistoryRequirement(trading_days=20)
    dates = required_trading_dates(_CALENDAR, as_of=_AS_OF, trading_days=20)
    store = FileHistoricalBarStore(root=tmp_path / "history")
    source = _seed_source(["A"], dates)
    service = _service(store, source)
    # pre-store all but one required day, leaving a single hole
    for d in dates[:-1]:
        store.upsert(
            HistoricalDailyBar(
                instrument_identity="NSE:A",
                trading_date=d,
                open=Decimal("100.10"),
                high=Decimal("110.20"),
                low=Decimal("95.30"),
                close=Decimal("105.40"),
                volume=1_000 + list(dates).index(d),
                source="fixture",
                ingested_at=_NOW,
            )
        )
    plan = service.plan_missing([_instrument("A")], requirement, as_of=_AS_OF)
    assert plan["NSE:A"] == (dates[-1],)  # only the single hole
    report = await service.backfill([_instrument("A")], requirement, as_of=_AS_OF)
    assert report.results[0].inserted == 1  # only the missing day fetched/stored


async def test_60_day_requirement_coverage(tmp_path: Path) -> None:
    requirement = HistoryRequirement(trading_days=60)
    dates = required_trading_dates(_CALENDAR, as_of=_AS_OF, trading_days=60)
    store = FileHistoricalBarStore(root=tmp_path / "history")
    service = _service(store, _seed_source(["A"], dates))
    report = await service.backfill([_instrument("A")], requirement, as_of=_AS_OF)
    assert report.results[0].inserted == 60 and report.results[0].complete_after
    assert service.coverage("NSE:A", requirement, as_of=_AS_OF).is_complete


def test_restart_durability_and_exact_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "history"
    fixture = HistoricalDailyBar(
        instrument_identity="NSE:TCS",
        trading_date=date(2026, 9, 11),
        open=Decimal("3400.05"),
        high=Decimal("3450.95"),
        low=Decimal("3399.05"),
        close=Decimal("3421.55"),
        volume=1234567,
        source="fixture",
        source_version="2026-09-11",
        ingested_at=_NOW,
    )
    first = FileHistoricalBarStore(root=root)
    assert first.upsert(fixture) is UpsertOutcome.INSERTED

    # simulate a full process restart: brand-new store object over the same directory
    restarted = FileHistoricalBarStore(root=root)
    series = restarted.read_range(
        "NSE:TCS", date(2026, 9, 11), date(2026, 9, 11), adjustment=PriceAdjustment.RAW
    )
    loaded = series.bars[0]
    assert loaded == fixture  # exact equality after durable round-trip
    assert isinstance(loaded.close, Decimal) and loaded.close == Decimal("3421.55")
    assert isinstance(loaded.volume, int) and loaded.volume == 1234567
    assert loaded.trading_date == date(2026, 9, 11)  # no date shifting
