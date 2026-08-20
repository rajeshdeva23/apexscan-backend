"""Previous Session Range % end-to-end tests (ADR-007 PSR spec; ADR-012 scanner/REST).

Drive the real runtime pipeline (historical warmup -> strategy -> scanner -> REST) over a
synthetic, network-free 208-instrument universe, enabling only
``previous_session_range_pct``. Proves DESCENDING ranking, partial/complete controls,
missing-history skip (no fabrication), canonical tie-break, and the generic REST contract.
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
from app.strategies.enums import StrategyLifecycleState as State

_UNIVERSE_SIZE = 208
_SYMBOLS = tuple(f"SYM{index:03d}" for index in range(_UNIVERSE_SIZE))
_INDEX = {symbol: index for index, symbol in enumerate(_SYMBOLS)}
_PRIMARY_UNAVAILABLE = frozenset({"SYM003", "SYM100", "SYM204"})
_REFERENCE = datetime(2026, 8, 6, 6, 0, tzinfo=UTC)
_OUT_OF_COVERAGE = datetime(2030, 6, 3, 6, 0, tzinfo=UTC)
_DB = "postgresql+asyncpg://user:pass@localhost:5432/apexscan"
_REDIS = "redis://localhost:6379/0"
_ERROR_THRESHOLD = 3
_STRATEGY = "previous_session_range_pct"


def _instrument(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _default_high(symbol: str) -> Decimal:
    """Strictly index-increasing high so range_pct = (index+1)/10 (ascending by index)."""
    return Decimal("100") + (Decimal(_INDEX[symbol] + 1) / Decimal(10))


def _expected_range_pct(symbol: str) -> Decimal:
    return Decimal(_INDEX[symbol] + 1) / Decimal(10)


def _ordered_descending(symbols: tuple[str, ...], unavailable: frozenset[str]) -> list[str]:
    """Valid symbols in DESCENDING range_pct order (== descending index), the expected rank."""
    return sorted(
        (s for s in symbols if s not in unavailable),
        key=lambda s: _INDEX[s],
        reverse=True,
    )


class _FakeDatabase:
    async def initialize(self, _url: str, *, echo: bool = False) -> None: ...
    async def verify_connectivity(self) -> None: ...
    async def dispose(self) -> None: ...


class _FakeRedis:
    async def initialize(self, _url: str) -> None: ...
    async def verify_connectivity(self) -> None: ...
    async def close(self) -> None: ...


class _RangeProvider:
    """Network-free provider over a universe with per-symbol range and missing-history modes."""

    capabilities = frozenset()

    def __init__(
        self,
        *,
        symbols: tuple[str, ...] = _SYMBOLS,
        empty_history_for: frozenset[str] = frozenset(),
        high_for: Callable[[str], Decimal] = _default_high,
        no_ticks: bool = False,
    ) -> None:
        self._symbols = symbols
        self._empty_history_for = empty_history_for
        self._high_for = high_for
        self._no_ticks = no_ticks
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
        high = self._high_for(symbol)
        candle = Candle(
            instrument=request.instrument,
            start_timestamp=request.start_timestamp,
            end_timestamp=request.start_timestamp + request.interval,
            open_price=Decimal("100"),
            high_price=high,
            low_price=Decimal("100"),
            close_price=Decimal("100"),
            traded_quantity=1000,
        )
        return HistoricalResult(request=request, candles=(candle,))

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        if not self._no_ticks:
            for instrument in request.instruments:
                yield Tick(
                    instrument=instrument,
                    event_timestamp=_REFERENCE,
                    last_price=Decimal("101.25"),
                    traded_quantity=10,
                )
        await self._gate.wait()


def _settings(symbols: tuple[str, ...] = _SYMBOLS) -> Settings:
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
    provider: _RangeProvider, *, clock_instant: datetime = _REFERENCE
) -> tuple[ApplicationLifecycle, LiveMarketRuntimeDependency, object]:
    dependency = LiveMarketRuntimeDependency(
        settings=_settings(),
        error_threshold=_ERROR_THRESHOLD,
        adapter=provider,  # type: ignore[arg-type]
        clock=ManualClock(clock_instant),
    )
    lifecycle = ApplicationLifecycle(_FakeDatabase(), _FakeRedis(), provider=dependency)
    app = create_app(lifecycle=lifecycle)
    await lifecycle.start(_app_settings())
    return lifecycle, dependency, app


def _runtime(dependency: LiveMarketRuntimeDependency) -> LiveMarketRuntime:
    composition = dependency._composition  # noqa: SLF001
    assert composition is not None
    return composition.runtime


def _state(dependency: LiveMarketRuntimeDependency) -> State:
    return _runtime(dependency)._strategy_lifecycle.state_of(_STRATEGY)  # noqa: SLF001


async def _wait_until(predicate: Callable[[], bool], *, limit: int = 200_000) -> None:
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


def _snapshot(dependency: LiveMarketRuntimeDependency) -> object:
    return _runtime(dependency).scanner_snapshot(_STRATEGY)


async def test_partial_universe_208_descending_end_to_end() -> None:
    provider = _RangeProvider(empty_history_for=_PRIMARY_UNAVAILABLE)
    lifecycle, dependency, app = await _start(provider)
    try:
        assert _state(dependency) is State.RUNNING
        await _wait_until(
            lambda: (s := _snapshot(dependency)) is not None and s.eligible_count == 205
        )
        snapshot = _snapshot(dependency)
        assert snapshot.expected_count == 208
        assert snapshot.evaluated_count == 205
        assert snapshot.eligible_count == 205
        expected = _ordered_descending(_SYMBOLS, _PRIMARY_UNAVAILABLE)
        assert [c.instrument.symbol for c in snapshot.candidates] == expected
        assert snapshot.candidates[0].instrument.symbol == "SYM207"  # largest range_pct = rank 1
        assert snapshot.candidates[0].ranking_metric_value == _expected_range_pct("SYM207")
        # missing instruments never fabricated
        present = {c.instrument.symbol for c in snapshot.candidates}
        assert present.isdisjoint(_PRIMARY_UNAVAILABLE)

        status, body = await _get(app, f"/api/v1/scanners/{_STRATEGY}")
        assert status == 200
        snap = body["snapshot"]
        assert snap["completeness"] == "partial"
        assert (snap["expected_count"], snap["evaluated_count"], snap["eligible_count"]) == (
            208,
            205,
            205,
        )
        assert snap["candidates"][0]["symbol"] == "SYM207"
        assert snap["candidates"][0]["ranking_metric_name"] == "previous_range_pct"
        assert isinstance(snap["candidates"][0]["ranking_metric_value"], str)
        assert "score" not in snap["candidates"][0]
    finally:
        await lifecycle.shutdown()


async def test_complete_universe_208() -> None:
    provider = _RangeProvider()
    lifecycle, dependency, _ = await _start(provider)
    try:
        await _wait_until(
            lambda: (s := _snapshot(dependency)) is not None and s.eligible_count == 208
        )
        snapshot = _snapshot(dependency)
        assert snapshot.completeness.value == "complete"
        assert snapshot.evaluated_count == 208
        assert snapshot.candidates[0].instrument.symbol == "SYM207"
    finally:
        await lifecycle.shutdown()


async def test_one_missing_control_207() -> None:
    provider = _RangeProvider(empty_history_for=frozenset({"SYM100"}))
    lifecycle, dependency, _ = await _start(provider)
    try:
        await _wait_until(
            lambda: (s := _snapshot(dependency)) is not None and s.eligible_count == 207
        )
        snapshot = _snapshot(dependency)
        assert snapshot.completeness.value == "partial"
        assert snapshot.expected_count == 208
        assert snapshot.evaluated_count == 207
        assert all(c.instrument.symbol != "SYM100" for c in snapshot.candidates)
        assert _state(dependency) is State.RUNNING
    finally:
        await lifecycle.shutdown()


async def test_zero_ready_control() -> None:
    provider = _RangeProvider(empty_history_for=frozenset(_SYMBOLS))
    lifecycle, dependency, _ = await _start(provider)
    try:
        await _wait_until(lambda: _runtime(dependency).status().ingestion_running)
        assert _state(dependency) is State.RUNNING
        # No MATCHED/NO_MATCH ever ingested -> no snapshot is fabricated.
        assert _snapshot(dependency) is None
    finally:
        await lifecycle.shutdown()


async def test_canonical_tie_break_on_equal_range_pct() -> None:
    symbols = ("SYM000", "SYM001", "SYM002")

    def equal_high(_symbol: str) -> Decimal:
        return Decimal("110")

    provider = _RangeProvider(symbols=symbols, high_for=equal_high)
    lifecycle, dependency, _ = await _start(provider)
    try:
        await _wait_until(
            lambda: (s := _snapshot(dependency)) is not None and s.eligible_count == 3
        )
        snapshot = _snapshot(dependency)
        # All equal range_pct -> canonical (exchange, symbol) ascending tie-break.
        assert [c.instrument.symbol for c in snapshot.candidates] == list(symbols)
        assert [c.rank for c in snapshot.candidates] == [1, 2, 3]
    finally:
        await lifecycle.shutdown()


async def test_rest_limit_projects_top_n_without_changing_counts() -> None:
    provider = _RangeProvider(empty_history_for=_PRIMARY_UNAVAILABLE)
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
        assert snap["candidates"][0]["symbol"] == "SYM207"
    finally:
        await lifecycle.shutdown()


async def test_global_failure_out_of_coverage_marks_error() -> None:
    provider = _RangeProvider(no_ticks=True)
    lifecycle, dependency, _ = await _start(provider, clock_instant=_OUT_OF_COVERAGE)
    try:
        await _wait_until(lambda: _state(dependency) in (State.ERROR, State.RUNNING))
        assert _state(dependency) is State.ERROR  # calendar out-of-coverage is a global failure
        assert _snapshot(dependency) is None
    finally:
        await lifecycle.shutdown()
