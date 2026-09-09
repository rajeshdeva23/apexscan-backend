"""Readiness integration over real Phase-E + Phase-F objects (DECOUPLING PHASE G).

Drives the readiness engine with a real UniverseSnapshot (Phase E) and a real file-backed
HistoricalService (Phase F, tmp_path) through per-strategy requirements, proving readiness differs
per (instrument, strategy) and across add/remove/re-add/mismatch/stale. No production wiring — the
Phase-E/F adapters live in the test. No strategy execution, no Dhan, no production infra.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.market_engine.clock import ManualClock
from app.market_engine.session import TradingCalendar
from app.market_history import (
    FileHistoricalBarStore,
    HistoricalDailyBar,
    HistoricalService,
    HistoryRequirement,
    InMemoryHistoricalDataSource,
    PriceAdjustment,
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
from app.strategy_readiness import (
    HistoricalNeed,
    ReadinessRequirement,
    StrategyReadinessEngine,
    StrategyReadinessStatus,
)

_NOW = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)
_AS_OF = date(2026, 9, 14)
_CALENDAR = TradingCalendar(holidays=[])


def _instrument(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


class _Sector:
    def resolve_primary(self, identity: str, on: date | None = None) -> str | None:
        return "IT"


def _snapshot(symbols: list[str], *, version_effective: date = _AS_OF) -> UniverseSnapshot:
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
        fno_effective_date=version_effective,
        instrument_master_source="d",
        instrument_master_version="v",
        sector_dataset_id="s",
        sector_version="1",
    )
    return resolver.resolve(
        [_instrument(s) for s in symbols],
        trading_date=version_effective,
        effective_at=_NOW,
        provenance=provenance,
    ).candidate


class _UniverseAdapter:
    """Phase-E snapshot -> engine UniverseView."""

    def __init__(self, snapshot: UniverseSnapshot, *, version: int) -> None:
        self._identities = set(snapshot.identities)
        self._version = version

    def is_member(self, instrument_identity: str) -> bool:
        return instrument_identity in self._identities

    def current_universe_version(self) -> int:
        return self._version


class _CoverageAdapter:
    """Phase-F HistoricalService -> engine HistoricalCoverageView."""

    def __init__(self, service: HistoricalService) -> None:
        self._service = service

    def coverage(self, instrument_identity, *, trading_days, adjustment, as_of):
        return self._service.coverage(
            instrument_identity,
            HistoryRequirement(trading_days=trading_days),
            as_of=as_of,
            adjustment=adjustment,
        )

    def has_previous_trading_bar(self, instrument_identity, *, as_of):
        return self._service.previous_trading_bar(instrument_identity, as_of=as_of) is not None


class _Live:
    def __init__(self) -> None:
        self.prev_close: dict[str, Decimal] = {}
        self.sess_open: dict[str, Decimal] = {}
        self.last: dict[str, Decimal] = {}
        self.observed: dict[str, datetime] = {}
        self.data_version: dict[str, int] = {}

    def previous_close(self, i):
        return self.prev_close.get(i)

    def session_open(self, i):
        return self.sess_open.get(i)

    def last_price(self, i):
        return self.last.get(i)

    def observed_at(self, i):
        return self.observed.get(i)

    def data_universe_version(self, i):
        return self.data_version.get(i)


def _bar(symbol: str, d: date) -> HistoricalDailyBar:
    return HistoricalDailyBar(
        instrument_identity=f"NSE:{symbol}",
        trading_date=d,
        open=Decimal("100.10"),
        high=Decimal("110.20"),
        low=Decimal("95.30"),
        close=Decimal("105.40"),
        volume=1000,
        source="fixture",
        ingested_at=_NOW,
    )


async def _seed(store: FileHistoricalBarStore, symbol: str, n: int) -> None:
    dates = required_trading_dates(_CALENDAR, as_of=_AS_OF, trading_days=n)
    store.upsert_many(_bar(symbol, d) for d in dates)


async def test_readiness_differs_per_instrument_and_strategy(tmp_path: Path) -> None:
    store = FileHistoricalBarStore(root=tmp_path / "h")
    await _seed(store, "A", 60)
    await _seed(store, "B", 20)
    await _seed(store, "C", 2)  # previous trading day + today (enough for previous-day-only)
    # D: none
    service = HistoricalService(
        store=store, source=InMemoryHistoricalDataSource({}), calendar=_CALENDAR, now=lambda: _NOW
    )
    snapshot = _snapshot(["A", "B", "C", "D"])
    live = _Live()
    for symbol in ("A", "B", "C", "D"):
        live.prev_close[f"NSE:{symbol}"] = Decimal("100")
        live.sess_open[f"NSE:{symbol}"] = Decimal("101")
        live.last[f"NSE:{symbol}"] = Decimal("102")
        live.observed[f"NSE:{symbol}"] = _NOW
    engine = StrategyReadinessEngine(
        universe=_UniverseAdapter(snapshot, version=snapshot.universe_version),
        coverage=_CoverageAdapter(service),
        live=live,
        clock=ManualClock(_NOW),
    )

    s1 = ReadinessRequirement(needs_previous_day_ohlc=True)  # previous-day only
    s2 = ReadinessRequirement(historical=HistoricalNeed(trading_days=20))
    s3 = ReadinessRequirement(
        historical=HistoricalNeed(trading_days=60), needs_previous_close=True, needs_last_price=True
    )

    def status(identity: str, req: ReadinessRequirement) -> StrategyReadinessStatus:
        return engine.evaluate(identity, "s", req, trading_date=_AS_OF).status

    # A: full history -> ready for all three
    assert status("NSE:A", s2) is StrategyReadinessStatus.READY
    assert status("NSE:A", s3) is StrategyReadinessStatus.READY
    # B: 20 days -> ready for s2, missing for s3 (needs 60)
    assert status("NSE:B", s2) is StrategyReadinessStatus.READY
    assert status("NSE:B", s3) is StrategyReadinessStatus.MISSING_HISTORY
    # C: only previous day -> ready for s1, missing for s2/s3
    assert status("NSE:C", s1) is StrategyReadinessStatus.READY
    assert status("NSE:C", s2) is StrategyReadinessStatus.MISSING_HISTORY
    # D: no history -> missing for everything historical
    assert status("NSE:D", s1) is StrategyReadinessStatus.MISSING_HISTORY
    assert status("NSE:D", s2) is StrategyReadinessStatus.MISSING_HISTORY


async def test_add_remove_readd_and_mismatch_and_stale(tmp_path: Path) -> None:
    store = FileHistoricalBarStore(root=tmp_path / "h")
    await _seed(store, "A", 20)
    await _seed(store, "D", 20)
    service = HistoricalService(
        store=store, source=InMemoryHistoricalDataSource({}), calendar=_CALENDAR, now=lambda: _NOW
    )
    req = ReadinessRequirement(historical=HistoricalNeed(trading_days=20), needs_last_price=True)

    def engine_for(symbols: list[str], live: _Live, *, version: int = 1) -> StrategyReadinessEngine:
        snap = _snapshot(symbols)
        return StrategyReadinessEngine(
            universe=_UniverseAdapter(snap, version=version),
            coverage=_CoverageAdapter(service),
            live=live,
            clock=ManualClock(_NOW),
        )

    live = _Live()
    live.last["NSE:D"] = Decimal("102")
    live.observed["NSE:D"] = _NOW

    # D present + history + live -> READY
    ready = engine_for(["A", "D"], live).evaluate("NSE:D", "s", req, trading_date=_AS_OF)
    assert ready.status is StrategyReadinessStatus.READY

    # D removed from universe -> REMOVED, but history is retained on disk
    removed = engine_for(["A"], live).evaluate("NSE:D", "s", req, trading_date=_AS_OF)
    assert removed.status is StrategyReadinessStatus.REMOVED
    assert len(store.available_dates("NSE:D", adjustment=PriceAdjustment.RAW)) == 20

    # D re-added -> reuses retained history -> READY again (no re-fetch)
    readded = engine_for(["A", "D"], live).evaluate("NSE:D", "s", req, trading_date=_AS_OF)
    assert readded.status is StrategyReadinessStatus.READY

    # universe mismatch: D's data tagged with a different version
    mismatch_live = _Live()
    mismatch_live.last["NSE:D"] = Decimal("102")
    mismatch_live.observed["NSE:D"] = _NOW
    mismatch_live.data_version["NSE:D"] = 999
    mismatch = engine_for(["A", "D"], mismatch_live, version=1).evaluate(
        "NSE:D", "s", req, trading_date=_AS_OF
    )
    assert mismatch.status is StrategyReadinessStatus.UNIVERSE_MISMATCH

    # stale live data
    stale_live = _Live()
    stale_live.last["NSE:D"] = Decimal("102")
    stale_live.observed["NSE:D"] = _NOW - timedelta(hours=1)
    stale_req = ReadinessRequirement(
        historical=HistoricalNeed(trading_days=20),
        needs_last_price=True,
        max_live_age=timedelta(minutes=5),
    )
    stale = engine_for(["A", "D"], stale_live).evaluate(
        "NSE:D", "s", stale_req, trading_date=_AS_OF
    )
    assert stale.status is StrategyReadinessStatus.STALE
