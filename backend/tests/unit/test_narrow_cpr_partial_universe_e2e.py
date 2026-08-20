"""Final offline end-to-end validation of partial-universe Narrow CPR (ADR-007 addendum).

Drives the real governed runtime over a synthetic 208-instrument NSE cash-equity universe
with NO manually seeded scanner results/candidates. The path is the production one:

``Settings.strategies_enabled`` -> ``StrategyCatalog`` -> ``NarrowCprStrategy`` ->
``StrategyManager.start`` (session ``HistoricalRequirement(lookback=1)``) -> historical
warmup -> per-instrument ``HistoricalContext`` -> ``assess_readiness`` ->
``NarrowCprStrategy.evaluate`` -> ``StrategyResultsPublished`` ->
``CrossInstrumentStrategyScanner`` (ascending ``cpr_width_pct``) -> ``ScannerSnapshot`` ->
``GET /api/v1/scanners/narrow_cpr``.

Every ``cpr_width_pct`` comes from real per-symbol previous-session candles built on the
identity ``H + L + C == 300 => pivot == 100 => cpr_width_pct == abs(close - 100)``. Each
instrument ``SYM{index}`` gets ``close == 100 + (index + 1) / 10`` so widths are strictly
increasing with index (0.10, 0.20, ...): the ascending ranking equals index order, giving a
unique narrowest (rank 1). Unavailable instruments (empty history or a boundary source
error) are left with no ``previous_session`` and are skipped per-context — never fabricated.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

from httpx import ASGITransport, AsyncClient, Response

from app.adapters.base import ProviderBoundaryError
from app.adapters.dhan.models import DhanCashEquityLiveUniverse, DhanInstrumentReference
from app.core.config import Settings
from app.core.lifecycle import ApplicationLifecycle
from app.main import create_app
from app.market_engine.clock import ManualClock
from app.market_engine.historical.requirements import HistoricalRequirement
from app.market_engine.timeframe import Timeframe
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
from app.services.market_runtime import LiveMarketRuntime, RuntimeState
from app.strategies.enums import StrategyLifecycleState as State
from app.strategy_manager.events import StrategyResultsPublished

_UNIVERSE_SIZE = 208
_SYMBOLS = tuple(f"SYM{index:03d}" for index in range(_UNIVERSE_SIZE))
_INDEX = {symbol: index for index, symbol in enumerate(_SYMBOLS)}
# Scattered unavailable set for the primary 205/3 split (interspersed to prove ranking skips).
_PRIMARY_UNAVAILABLE = frozenset({"SYM003", "SYM100", "SYM204"})

# 2026-08-06 06:00 UTC == 11:30 IST Thursday: mid regular session (a tick is LIVE_SESSION and
# warmup resolves the previous session 2026-08-05). 2030 is outside the 2026 dataset coverage.
_REFERENCE = datetime(2026, 8, 6, 6, 0, tzinfo=UTC)
_TRADING_DATE = date(2026, 8, 6)
_OUT_OF_COVERAGE = datetime(2030, 6, 3, 6, 0, tzinfo=UTC)
_DB = "postgresql+asyncpg://user:pass@localhost:5432/apexscan"
_REDIS = "redis://localhost:6379/0"
_ERROR_THRESHOLD = 3


def _instrument(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _close_for(symbol: str) -> Decimal:
    return Decimal("100") + (Decimal(_INDEX[symbol] + 1) / Decimal(10))


def _expected_width(symbol: str) -> Decimal:
    return Decimal(_INDEX[symbol] + 1) / Decimal(10)


def _ordered_valid(unavailable: frozenset[str]) -> list[str]:
    """Valid symbols in ascending-width (== index) order — the expected candidate ranking."""
    return [symbol for symbol in _SYMBOLS if symbol not in unavailable]


class _FakeDatabase:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def initialize(self, _url: str, *, echo: bool = False) -> None:
        self.events.append("database.initialize")

    async def verify_connectivity(self) -> None:
        self.events.append("database.verify")

    async def dispose(self) -> None:
        self.events.append("database.dispose")


class _FakeRedis:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def initialize(self, _url: str) -> None:
        self.events.append("redis.initialize")

    async def verify_connectivity(self) -> None:
        self.events.append("redis.verify")

    async def close(self) -> None:
        self.events.append("redis.close")


class _LargeFakeProvider:
    """A network-free provider over a configurable universe with per-symbol failure modes."""

    capabilities = frozenset()

    def __init__(
        self,
        *,
        symbols: tuple[str, ...] = _SYMBOLS,
        empty_history_for: frozenset[str] = frozenset(),
        source_error_for: frozenset[str] = frozenset(),
        tick_order: tuple[str, ...] | None = None,
        no_ticks: bool = False,
    ) -> None:
        self._symbols = symbols
        self._empty_history_for = empty_history_for
        self._source_error_for = source_error_for
        self._tick_order = tick_order if tick_order is not None else symbols
        self._no_ticks = no_ticks
        self._gate = asyncio.Event()
        self.stream_calls = 0
        self.historical_calls = 0
        self.disconnected = asyncio.Event()

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnected.set()

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
        """Return one authoritative previous-session candle, or a per-symbol local failure."""
        self.historical_calls += 1
        symbol = request.instrument.symbol
        if symbol in self._source_error_for:
            raise ProviderBoundaryError(f"boundary source failure for {symbol}")
        if symbol in self._empty_history_for:
            return HistoricalResult(request=request, candles=())
        close = _close_for(symbol)
        candle = Candle(
            instrument=request.instrument,
            start_timestamp=request.start_timestamp,
            end_timestamp=request.start_timestamp + request.interval,
            open_price=Decimal("100"),
            high_price=Decimal("140"),
            low_price=Decimal("160") - close,
            close_price=close,
            traded_quantity=1000,
        )
        return HistoricalResult(request=request, candles=(candle,))

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        """Yield one live tick per requested instrument (controlled order), then block."""
        self.stream_calls += 1
        if not self._no_ticks:
            by_symbol = {inst.symbol: inst for inst in request.instruments}
            for symbol in self._tick_order:
                instrument = by_symbol.get(symbol)
                if instrument is None:
                    continue
                yield Tick(
                    instrument=instrument,
                    event_timestamp=_REFERENCE,
                    last_price=Decimal("101.25"),
                    traded_quantity=10,
                )
        await self._gate.wait()


def _provider_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "development",
        "database_url": _DB,
        "redis_url": _REDIS,
        "market_provider_enabled": True,
        "dhan_auth_mode": "totp",
        "dhan_client_id": "client-id",
        "dhan_pin": "123456",
        "dhan_totp_secret": "totp-secret",
        "strategies_enabled": "narrow_cpr",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _app_settings() -> Settings:
    return Settings(app_env="development", database_url=_DB, redis_url=_REDIS)


async def _start(
    provider: _LargeFakeProvider, *, clock_instant: datetime = _REFERENCE
) -> tuple[ApplicationLifecycle, LiveMarketRuntimeDependency, object]:
    dependency = LiveMarketRuntimeDependency(
        settings=_provider_settings(),
        error_threshold=_ERROR_THRESHOLD,
        adapter=provider,  # type: ignore[arg-type]
        clock=ManualClock(clock_instant),
    )
    lifecycle = ApplicationLifecycle(_FakeDatabase([]), _FakeRedis([]), provider=dependency)
    app = create_app(lifecycle=lifecycle)
    await lifecycle.start(_app_settings())
    return lifecycle, dependency, app


def _runtime(dependency: LiveMarketRuntimeDependency) -> LiveMarketRuntime:
    composition = dependency._composition  # noqa: SLF001
    assert composition is not None
    return composition.runtime


def _lifecycle_state(dependency: LiveMarketRuntimeDependency, strategy_id: str) -> State:
    return _runtime(dependency)._strategy_lifecycle.state_of(strategy_id)  # noqa: SLF001


async def _wait_until(predicate: object, *, limit: int = 100_000) -> None:
    for _ in range(limit):
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not met in time")


async def _wait_eligible(dependency: LiveMarketRuntimeDependency, count: int) -> None:
    def _ready() -> bool:
        snapshot = dependency.scanner_snapshot("narrow_cpr")
        return snapshot is not None and snapshot.eligible_count == count

    await _wait_until(_ready)


async def _settle(iterations: int = 1000) -> None:
    for _ in range(iterations):
        await asyncio.sleep(0)


async def _get(app: object, path: str) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# --------------------------------------------------------------------------- #
# §2/§3/§4/§5/§7/§17 — the primary 208-instrument partial-universe scenario.
# --------------------------------------------------------------------------- #
async def test_partial_universe_208_end_to_end() -> None:
    unavailable = _PRIMARY_UNAVAILABLE
    valid_order = _ordered_valid(unavailable)
    assert len(valid_order) == 205
    provider = _LargeFakeProvider(empty_history_for=unavailable)
    lifecycle, dependency, app = await _start(provider)
    runtime = _runtime(dependency)
    try:
        # §9 historical demand: the session requirement entered the effective union.
        reqs = runtime.historical_requirements.effective_requirements()
        assert HistoricalRequirement(timeframe=Timeframe.session(), lookback=1) in reqs

        await _wait_eligible(dependency, 205)
        # §8 strategy RUNNING despite three un-warmable instruments.
        assert _lifecycle_state(dependency, "narrow_cpr") is State.RUNNING

        snapshot = dependency.scanner_snapshot("narrow_cpr")
        assert snapshot is not None
        # §12/§13/§14 counts.
        assert snapshot.expected_count == 208
        assert snapshot.evaluated_count == 205
        assert snapshot.eligible_count == 205
        assert snapshot.completeness.value == "partial"
        assert snapshot.trading_date == _TRADING_DATE

        symbols = [c.instrument.symbol for c in snapshot.candidates]
        assert len(symbols) == 205
        # §7 strictly ascending cpr_width_pct == index order; §17 unique narrowest at rank 1.
        assert symbols == valid_order
        assert [c.rank for c in snapshot.candidates] == list(range(1, 206))
        assert snapshot.candidates[0].instrument.symbol == valid_order[0]
        # §4 no fabrication: failed instruments are simply absent — no width, no rank.
        assert _PRIMARY_UNAVAILABLE.isdisjoint(symbols)
        # Metric values are real (computed from the OHLC identity), not injected.
        for candidate in snapshot.candidates:
            assert candidate.ranking_metric_name == "cpr_width_pct"
        assert Decimal(str(snapshot.candidates[0].ranking_metric_value)) == _expected_width(
            valid_order[0]
        )
        assert Decimal(str(snapshot.candidates[-1].ranking_metric_value)) == _expected_width(
            valid_order[-1]
        )

        # §5 REST exposes PARTIAL honestly.
        response = await _get(app, "/api/v1/scanners/narrow_cpr")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()["snapshot"]
        assert body is not None
        assert body["expected_count"] == 208
        assert body["evaluated_count"] == 205
        assert body["eligible_count"] == 205
        assert body["completeness"] == "partial"
        rest_symbols = [c["symbol"] for c in body["candidates"]]
        assert rest_symbols == valid_order
        assert _PRIMARY_UNAVAILABLE.isdisjoint(rest_symbols)
        assert "score" not in body["candidates"][0]  # narrow_cpr score stays None
    finally:
        await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# §6 — REST limit projects the top-N by rank; the counts still describe 208/205/205.
# --------------------------------------------------------------------------- #
async def test_rest_limit_projects_top_n_without_changing_counts() -> None:
    provider = _LargeFakeProvider(empty_history_for=_PRIMARY_UNAVAILABLE)
    lifecycle, dependency, app = await _start(provider)
    try:
        await _wait_eligible(dependency, 205)
        response = await _get(app, "/api/v1/scanners/narrow_cpr?limit=20")
        assert response.status_code == 200
        body = response.json()["snapshot"]
        assert len(body["candidates"]) == 20
        assert [c["symbol"] for c in body["candidates"]] == _ordered_valid(_PRIMARY_UNAVAILABLE)[
            :20
        ]
        assert body["expected_count"] == 208
        assert body["evaluated_count"] == 205
        assert body["eligible_count"] == 205
        # The internal snapshot still holds all 205 candidates.
        snapshot = dependency.scanner_snapshot("narrow_cpr")
        assert snapshot is not None and len(snapshot.candidates) == 205
    finally:
        await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# §8 — COMPLETE control: all 208 histories valid → 208/208 COMPLETE (path undamaged).
# --------------------------------------------------------------------------- #
async def test_complete_208_control() -> None:
    provider = _LargeFakeProvider()
    lifecycle, dependency, _app = await _start(provider)
    try:
        await _wait_eligible(dependency, 208)
        assert _lifecycle_state(dependency, "narrow_cpr") is State.RUNNING
        snapshot = dependency.scanner_snapshot("narrow_cpr")
        assert snapshot is not None
        assert snapshot.expected_count == 208
        assert snapshot.evaluated_count == 208
        assert snapshot.eligible_count == 208
        assert snapshot.completeness.value == "complete"
        assert [c.instrument.symbol for c in snapshot.candidates] == list(_SYMBOLS)
    finally:
        await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# §9 / §13 — 207 valid, one missing (no completed previous session): RUNNING, PARTIAL 207.
# --------------------------------------------------------------------------- #
async def test_one_missing_control() -> None:
    unavailable = frozenset({"SYM100"})
    provider = _LargeFakeProvider(empty_history_for=unavailable)
    lifecycle, dependency, _app = await _start(provider)
    try:
        await _wait_eligible(dependency, 207)
        assert _lifecycle_state(dependency, "narrow_cpr") is State.RUNNING  # no global ERROR
        snapshot = dependency.scanner_snapshot("narrow_cpr")
        assert snapshot is not None
        assert snapshot.expected_count == 208
        assert snapshot.evaluated_count == 207
        assert snapshot.completeness.value == "partial"
        symbols = [c.instrument.symbol for c in snapshot.candidates]
        assert "SYM100" not in symbols  # missing instrument absent; others continue
        assert symbols == _ordered_valid(unavailable)
    finally:
        await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# §10 — zero-ready control: warmup infrastructure succeeds, 0/208 locally ready.
# The strategy still reaches RUNNING (governance); no terminal result is published, so the
# scanner emits no snapshot (reported as actual behavior — nothing is fabricated).
# --------------------------------------------------------------------------- #
async def test_zero_ready_control() -> None:
    provider = _LargeFakeProvider(empty_history_for=frozenset(_SYMBOLS))
    lifecycle, dependency, _app = await _start(provider)
    try:
        await _wait_until(lambda: provider.stream_calls == 1)
        await _settle()
        assert _lifecycle_state(dependency, "narrow_cpr") is State.RUNNING
        assert dependency.scanner_snapshot("narrow_cpr") is None  # no fabricated snapshot
        assert _runtime(dependency).status().ingestion_running  # runtime healthy
    finally:
        await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# §11 — global failure control: an out-of-coverage reference makes warmup raise
# (OutsideCalendarCoverageError). Partial-universe semantics must NOT swallow it.
# --------------------------------------------------------------------------- #
async def test_global_failure_control() -> None:
    provider = _LargeFakeProvider(no_ticks=True)
    lifecycle, dependency, _app = await _start(provider, clock_instant=_OUT_OF_COVERAGE)
    try:
        assert _lifecycle_state(dependency, "narrow_cpr") is State.ERROR
        assert dependency.scanner_snapshot("narrow_cpr") is None  # no candidates on global failure
    finally:
        await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# §12 — local source failure control: three symbols raise a boundary source error that the
# bridge maps to HistoricalSourceError and warmup catches per-plan (real locality).
# --------------------------------------------------------------------------- #
async def test_local_source_failure_control() -> None:
    failed = frozenset({"SYM010", "SYM050", "SYM150"})
    provider = _LargeFakeProvider(source_error_for=failed)
    lifecycle, dependency, _app = await _start(provider)
    try:
        await _wait_eligible(dependency, 205)
        assert _lifecycle_state(dependency, "narrow_cpr") is State.RUNNING
        snapshot = dependency.scanner_snapshot("narrow_cpr")
        assert snapshot is not None
        assert snapshot.expected_count == 208
        assert snapshot.evaluated_count == 205
        assert snapshot.completeness.value == "partial"
        assert failed.isdisjoint(c.instrument.symbol for c in snapshot.candidates)
    finally:
        await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# §14 — multi-strategy isolation is proved strategy-agnostically at the manager level in
# tests/unit/strategy_manager/test_requirements_lifecycle.py::
# test_multi_strategy_partial_universe_isolation. The E2E composition intentionally ships
# only narrow_cpr (production_catalog), so an E2E second strategy would require a production
# change (a catalog seam), which is out of scope for this validation phase.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# §15 — the REST read path triggers no provider/historical/evaluation work.
# --------------------------------------------------------------------------- #
async def test_api_read_path_is_pure() -> None:
    provider = _LargeFakeProvider(empty_history_for=_PRIMARY_UNAVAILABLE)
    lifecycle, dependency, app = await _start(provider)
    runtime = _runtime(dependency)
    published: list[StrategyResultsPublished] = []
    runtime.bus.subscribe(StrategyResultsPublished, published.append)
    try:
        await _wait_eligible(dependency, 205)
        stream_calls = provider.stream_calls
        historical_calls = provider.historical_calls
        published_count = len(published)
        for _ in range(5):
            assert (await _get(app, "/api/v1/scanners/narrow_cpr")).status_code == 200
        assert provider.stream_calls == stream_calls
        assert provider.historical_calls == historical_calls
        assert len(published) == published_count
    finally:
        await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# §16 — arrival order does not change the final ranking or the missing set.
# --------------------------------------------------------------------------- #
async def _partial_rest_json(tick_order: tuple[str, ...] | None) -> object:
    provider = _LargeFakeProvider(empty_history_for=_PRIMARY_UNAVAILABLE, tick_order=tick_order)
    lifecycle, dependency, app = await _start(provider)
    try:
        await _wait_eligible(dependency, 205)
        return (await _get(app, "/api/v1/scanners/narrow_cpr")).json()
    finally:
        await lifecycle.shutdown()


async def test_arrival_order_does_not_change_ranking() -> None:
    forward = await _partial_rest_json(_SYMBOLS)
    reversed_order = await _partial_rest_json(tuple(reversed(_SYMBOLS)))
    forward_symbols = [c["symbol"] for c in forward["snapshot"]["candidates"]]  # type: ignore[index]
    reversed_symbols = [c["symbol"] for c in reversed_order["snapshot"]["candidates"]]  # type: ignore[index]
    assert forward_symbols == reversed_symbols == _ordered_valid(_PRIMARY_UNAVAILABLE)


# --------------------------------------------------------------------------- #
# §17 — two identical runs yield identical ranking, metric values, and REST JSON.
# --------------------------------------------------------------------------- #
async def test_repeat_runs_are_deterministic() -> None:
    assert await _partial_rest_json(None) == await _partial_rest_json(None)


# --------------------------------------------------------------------------- #
# §18 — governed shutdown after a partial run: runtime SHUTDOWN, provider disconnected,
# ingestion/refresh stopped, scanner read surface withdrawn, no orphan tasks.
# --------------------------------------------------------------------------- #
async def test_clean_shutdown_after_partial_run() -> None:
    provider = _LargeFakeProvider(empty_history_for=_PRIMARY_UNAVAILABLE)
    lifecycle, dependency, _app = await _start(provider)
    runtime = _runtime(dependency)
    await _wait_eligible(dependency, 205)
    await lifecycle.shutdown()
    assert provider.disconnected.is_set()
    status = runtime.status()
    assert status.state is RuntimeState.SHUTDOWN
    assert not status.ingestion_running
    assert not status.refresh_driver_running
    assert not dependency.scanner_read_available()
