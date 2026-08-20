"""Previous Session Body % end-to-end tests (ADR-007 PSB spec; ADR-012 scanner/REST).

Drive the real runtime pipeline (historical warmup -> strategy -> scanner -> REST) over a
synthetic, network-free 208-instrument universe, enabling only
``previous_session_body_pct``. Proves DESCENDING ranking by absolute body %, direction
neutrality (a down session can outrank an up session by magnitude), partial/complete
controls, missing-history skip (no fabrication), canonical tie-break, and the REST contract.

OHLC design: open=100, high=200, low=1 for every instrument (so range % is constant and
cannot determine ordering), and close = 100 ± (index+1)/10 alternating up/down by parity, so
``body_pct = (index+1)/10`` strictly increases with index while direction alternates.
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
_STRATEGY = "previous_session_body_pct"


def _instrument(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _default_close(symbol: str) -> Decimal:
    """close = 100 +/- (index+1)/10 alternating up/down by parity -> body_pct = (index+1)/10."""
    magnitude = Decimal(_INDEX[symbol] + 1) / Decimal(10)
    return Decimal("100") + magnitude if _INDEX[symbol] % 2 == 0 else Decimal("100") - magnitude


def _expected_body_pct(symbol: str) -> Decimal:
    return Decimal(_INDEX[symbol] + 1) / Decimal(10)


def _ordered_descending(symbols: tuple[str, ...], unavailable: frozenset[str]) -> list[str]:
    return sorted(
        (s for s in symbols if s not in unavailable), key=lambda s: _INDEX[s], reverse=True
    )


class _FakeDatabase:
    async def initialize(self, _url: str, *, echo: bool = False) -> None: ...
    async def verify_connectivity(self) -> None: ...
    async def dispose(self) -> None: ...


class _FakeRedis:
    async def initialize(self, _url: str) -> None: ...
    async def verify_connectivity(self) -> None: ...
    async def close(self) -> None: ...


class _BodyProvider:
    """Network-free provider over a universe with per-symbol close and missing-history modes."""

    capabilities = frozenset()

    def __init__(
        self,
        *,
        symbols: tuple[str, ...] = _SYMBOLS,
        empty_history_for: frozenset[str] = frozenset(),
        close_for: Callable[[str], Decimal] = _default_close,
    ) -> None:
        self._symbols = symbols
        self._empty_history_for = empty_history_for
        self._close_for = close_for
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
        candle = Candle(
            instrument=request.instrument,
            start_timestamp=request.start_timestamp,
            end_timestamp=request.start_timestamp + request.interval,
            open_price=Decimal("100"),
            high_price=Decimal("200"),
            low_price=Decimal("1"),
            close_price=self._close_for(symbol),
            traded_quantity=1000,
        )
        return HistoricalResult(request=request, candles=(candle,))

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
    provider: _BodyProvider,
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


async def test_partial_universe_208_descending_direction_neutral() -> None:
    provider = _BodyProvider(empty_history_for=_PRIMARY_UNAVAILABLE)
    lifecycle, dependency, app = await _start(provider)
    try:
        await _wait_until(
            lambda: (s := _snapshot(dependency)) is not None and s.eligible_count == 205
        )
        snapshot = _snapshot(dependency)
        assert snapshot.expected_count == 208
        assert snapshot.evaluated_count == 205
        assert snapshot.completeness.value == "partial"
        expected = _ordered_descending(_SYMBOLS, _PRIMARY_UNAVAILABLE)
        assert [c.instrument.symbol for c in snapshot.candidates] == expected
        # rank 1 = SYM207 (largest body %); SYM207 is a DOWN session (odd index) yet ranks first
        # purely by magnitude -> direction-neutral ranking.
        assert snapshot.candidates[0].instrument.symbol == "SYM207"
        assert snapshot.candidates[0].ranking_metric_value == _expected_body_pct("SYM207")
        assert _default_close("SYM207") < Decimal("100")  # confirm rank 1 is a down candle
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
        assert snap["candidates"][0]["ranking_metric_name"] == "previous_body_pct"
        assert isinstance(snap["candidates"][0]["ranking_metric_value"], str)
    finally:
        await lifecycle.shutdown()


async def test_complete_universe_208() -> None:
    lifecycle, dependency, _ = await _start(_BodyProvider())
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
    lifecycle, dependency, _ = await _start(_BodyProvider(empty_history_for=frozenset({"SYM100"})))
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
    lifecycle, dependency, _ = await _start(_BodyProvider(empty_history_for=frozenset(_SYMBOLS)))
    try:
        await _wait_until(lambda: _runtime(dependency).status().ingestion_running)
        assert (
            _snapshot(dependency) is None
        )  # no MATCHED/NO_MATCH ingested -> no fabricated snapshot
    finally:
        await lifecycle.shutdown()


async def test_canonical_tie_break_on_equal_body_pct() -> None:
    symbols = ("SYM000", "SYM001", "SYM002")

    def equal_close(_symbol: str) -> Decimal:
        return Decimal("110")  # all: open 100 -> body 10 -> body_pct 10

    lifecycle, dependency, _ = await _start(_BodyProvider(symbols=symbols, close_for=equal_close))
    try:
        await _wait_until(
            lambda: (s := _snapshot(dependency)) is not None and s.eligible_count == 3
        )
        snapshot = _snapshot(dependency)
        assert [c.instrument.symbol for c in snapshot.candidates] == list(symbols)
        assert [c.rank for c in snapshot.candidates] == [1, 2, 3]
    finally:
        await lifecycle.shutdown()


async def test_rest_limit_projects_top_n_without_changing_counts() -> None:
    provider = _BodyProvider(empty_history_for=_PRIMARY_UNAVAILABLE)
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
