"""Previous Session Relative Range end-to-end tests (ADR-007 PSRR; ADR-012 scanner/REST).

Drive the real runtime pipeline (multi-session historical warmup -> strategy -> scanner ->
REST) over a synthetic 208-instrument universe, enabling only
``previous_session_relative_range``. Each instrument's provider history is 21 day-candles:
the 20 baseline sessions have range 10 (range_pct 10 -> median 10) and the newest session
(D-1) has range = (index+1)/10, so ``relative_range_ratio = (index+1)/100`` — strictly
increasing with index. ASCENDING ranking => rank 1 = SYM000 (smallest ratio).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from decimal import Decimal

from httpx import ASGITransport, AsyncClient

from app.adapters.dhan.models import DhanCashEquityLiveUniverse, DhanInstrumentReference
from app.core.config import Settings
from app.core.lifecycle import ApplicationLifecycle
from app.main import create_app
from app.market_engine.clock import ManualClock
from app.schemas.market_data import (
    Candle,
    HistoricalRequest,
    HistoricalResult,
    Instrument,
    MarketData,
    ProviderHealth,
    ProviderStatus,
    SubscriptionRequest,
    Tick,
)
from app.services.dhan_runtime_composition import LiveMarketRuntimeDependency
from app.services.market_runtime import LiveMarketRuntime

_UNIVERSE_SIZE = 208
_SYMBOLS = tuple(f"SYM{index:03d}" for index in range(_UNIVERSE_SIZE))
_INDEX = {symbol: index for index, symbol in enumerate(_SYMBOLS)}
_PRIMARY_UNAVAILABLE = frozenset({"SYM003", "SYM100", "SYM204"})
_REFERENCE = datetime(2026, 8, 6, 6, 0, tzinfo=UTC)
_DB = "postgresql+asyncpg://user:pass@localhost:5432/apexscan"
_REDIS = "redis://localhost:6379/0"
_ERROR_THRESHOLD = 3
_STRATEGY = "previous_session_relative_range"
_BASELINE_HIGH = Decimal("110")  # baseline range 10 -> range_pct 10 -> median 10


def _instrument(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _subject_range(symbol: str) -> Decimal:
    return Decimal(_INDEX[symbol] + 1) / Decimal(10)


def _expected_ratio(symbol: str) -> Decimal:
    return _subject_range(symbol) / Decimal(10)


def _ordered_ascending(symbols: tuple[str, ...], unavailable: frozenset[str]) -> list[str]:
    return sorted((s for s in symbols if s not in unavailable), key=lambda s: _INDEX[s])


class _FakeDatabase:
    async def initialize(self, _url: str, *, echo: bool = False) -> None: ...
    async def verify_connectivity(self) -> None: ...
    async def dispose(self) -> None: ...


class _FakeRedis:
    async def initialize(self, _url: str) -> None: ...
    async def verify_connectivity(self) -> None: ...
    async def close(self) -> None: ...


class _RelativeProvider:
    """Network-free provider: 21 day-candles/instrument, subject on the newest (D-1)."""

    capabilities = frozenset()

    def __init__(
        self,
        *,
        symbols: tuple[str, ...] = _SYMBOLS,
        empty_history_for: frozenset[str] = frozenset(),
    ) -> None:
        self._symbols = symbols
        self._empty_history_for = empty_history_for
        self._gate = asyncio.Event()

    async def connect(self) -> None: ...

    async def disconnect(self) -> None:
        self._gate.set()

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_REFERENCE)

    async def load_instruments(self) -> tuple[Instrument, ...]:
        return tuple(_instrument(symbol) for symbol in self._symbols)

    def load_nse_cash_equity_live_universe(self) -> DhanCashEquityLiveUniverse:
        return DhanCashEquityLiveUniverse(
            underlyings=(),
            cash_references=tuple(
                DhanInstrumentReference(
                    instrument=_instrument(symbol),
                    security_id=f"SEC-{symbol}",
                    underlying_security_id=None,
                    exchange_segment="NSE_EQ",
                    provider_instrument_type="ES",
                )
                for symbol in self._symbols
            ),
            missing_underlyings=(),
            ambiguous_underlyings=(),
            symbol_mismatches=(),
        )

    async def load_historical_data(self, request: HistoricalRequest) -> HistoricalResult:
        symbol = request.instrument.symbol
        if symbol in self._empty_history_for:
            return HistoricalResult(request=request, candles=())
        starts = []
        cursor = request.start_timestamp
        while cursor < request.end_timestamp:
            starts.append(cursor)
            cursor = cursor + request.interval
        newest = max(starts)  # canonicalises to D-1 (the newest trading day in the window)
        subject_high = Decimal("100") + _subject_range(symbol)
        candles = tuple(
            Candle(
                instrument=request.instrument,
                start_timestamp=start,
                end_timestamp=start + request.interval,
                open_price=Decimal("100"),
                high_price=subject_high if start == newest else _BASELINE_HIGH,
                low_price=Decimal("100"),
                close_price=Decimal("100"),
                traded_quantity=1000,
            )
            for start in starts
        )
        return HistoricalResult(request=request, candles=candles)

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        for instrument in request.instruments:
            yield Tick(
                instrument=instrument,
                event_timestamp=_REFERENCE,
                last_price=Decimal("101.25"),
                traded_quantity=10,
            )
        await self._gate.wait()


def _settings() -> Settings:
    return Settings(
        app_env="development",
        database_url=_DB,
        redis_url=_REDIS,
        market_provider_enabled=True,
        dhan_auth_mode="access_token",
        dhan_access_token="offline-unused",
        strategies_enabled=_STRATEGY,
    )


def _app_settings() -> Settings:
    return Settings(app_env="development", database_url=_DB, redis_url=_REDIS)


async def _start(
    provider: _RelativeProvider,
) -> tuple[ApplicationLifecycle, LiveMarketRuntimeDependency, object]:
    dependency = LiveMarketRuntimeDependency(
        settings=_settings(),
        error_threshold=_ERROR_THRESHOLD,
        adapter=provider,  # type: ignore[arg-type]
        clock=ManualClock(_REFERENCE),
    )
    lifecycle = ApplicationLifecycle(_FakeDatabase(), _FakeRedis(), provider=dependency)
    app = create_app(lifecycle=lifecycle)
    await lifecycle.start(_app_settings())
    return lifecycle, dependency, app


def _runtime(dependency: LiveMarketRuntimeDependency) -> LiveMarketRuntime:
    composition = dependency._composition  # noqa: SLF001
    assert composition is not None
    return composition.runtime


def _snapshot(dependency: LiveMarketRuntimeDependency) -> object:
    return _runtime(dependency).scanner_snapshot(_STRATEGY)


async def _wait_until(predicate: Callable[[], bool], *, limit: int = 400_000) -> None:
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not met in time")


async def _get(app: object, path: str) -> tuple[int, dict]:
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.get(path)
    return response.status_code, response.json()


async def test_partial_universe_ascending_rank1_smallest_ratio() -> None:
    provider = _RelativeProvider(empty_history_for=_PRIMARY_UNAVAILABLE)
    lifecycle, dependency, app = await _start(provider)
    try:
        await _wait_until(
            lambda: (s := _snapshot(dependency)) is not None and s.eligible_count == 205
        )
        snapshot = _snapshot(dependency)
        assert snapshot.expected_count == 208
        assert snapshot.evaluated_count == 205
        assert snapshot.completeness.value == "partial"
        expected = _ordered_ascending(_SYMBOLS, _PRIMARY_UNAVAILABLE)
        assert [c.instrument.symbol for c in snapshot.candidates] == expected
        assert snapshot.candidates[0].instrument.symbol == "SYM000"  # smallest ratio
        assert snapshot.candidates[0].ranking_metric_value == _expected_ratio("SYM000")
        assert {c.instrument.symbol for c in snapshot.candidates}.isdisjoint(_PRIMARY_UNAVAILABLE)

        status, body = await _get(app, f"/api/v1/scanners/{_STRATEGY}")
        assert status == 200
        snap = body["snapshot"]
        assert snap["completeness"] == "partial"
        assert (snap["expected_count"], snap["evaluated_count"], snap["eligible_count"]) == (
            208,
            205,
            205,
        )
        assert snap["candidates"][0]["symbol"] == "SYM000"
        assert snap["candidates"][0]["ranking_metric_name"] == "relative_range_ratio"
        assert isinstance(snap["candidates"][0]["ranking_metric_value"], str)
    finally:
        await lifecycle.shutdown()


async def test_complete_universe_208() -> None:
    lifecycle, dependency, _ = await _start(_RelativeProvider())
    try:
        await _wait_until(
            lambda: (s := _snapshot(dependency)) is not None and s.eligible_count == 208
        )
        snapshot = _snapshot(dependency)
        assert snapshot.completeness.value == "complete"
        assert snapshot.evaluated_count == 208
        assert snapshot.candidates[0].instrument.symbol == "SYM000"
    finally:
        await lifecycle.shutdown()


async def test_one_missing_control_207() -> None:
    lifecycle, dependency, _ = await _start(
        _RelativeProvider(empty_history_for=frozenset({"SYM100"}))
    )
    try:
        await _wait_until(
            lambda: (s := _snapshot(dependency)) is not None and s.eligible_count == 207
        )
        snapshot = _snapshot(dependency)
        assert snapshot.completeness.value == "partial"
        assert snapshot.evaluated_count == 207
        assert all(c.instrument.symbol != "SYM100" for c in snapshot.candidates)
    finally:
        await lifecycle.shutdown()


async def test_zero_ready_control() -> None:
    lifecycle, dependency, _ = await _start(
        _RelativeProvider(empty_history_for=frozenset(_SYMBOLS))
    )
    try:
        await _wait_until(lambda: _runtime(dependency).status().ingestion_running)
        assert _snapshot(dependency) is None  # no fabricated snapshot
    finally:
        await lifecycle.shutdown()


async def test_rest_limit_projects_top_n_without_changing_counts() -> None:
    provider = _RelativeProvider(empty_history_for=_PRIMARY_UNAVAILABLE)
    lifecycle, dependency, app = await _start(provider)
    try:
        await _wait_until(
            lambda: (s := _snapshot(dependency)) is not None and s.eligible_count == 205
        )
        status, body = await _get(app, f"/api/v1/scanners/{_STRATEGY}?limit=20")
        assert status == 200
        snap = body["snapshot"]
        assert len(snap["candidates"]) == 20
        assert (snap["expected_count"], snap["evaluated_count"], snap["eligible_count"]) == (
            208,
            205,
            205,
        )
        assert snap["candidates"][0]["symbol"] == "SYM000"
    finally:
        await lifecycle.shutdown()
