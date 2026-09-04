"""End-to-end validation of the Narrow CPR production vertical slice (ADR-007/010/012/013).

Drives the real governed flow with an offline fake provider (no network, no credentials):
``Settings.strategies_enabled`` -> ``StrategyCatalog`` -> ``NarrowCprStrategy`` ->
``StrategyManager.start`` (session ``HistoricalRequirement(lookback=1)``) -> historical warmup
-> ``previous_session`` -> ``NarrowCprStrategy.evaluate`` -> ``StrategyResultsPublished`` ->
``CrossInstrumentStrategyScanner`` (ascending ``cpr_width_pct``) -> ``ScannerSnapshot`` ->
``GET /api/v1/scanners/narrow_cpr``.

Every ``cpr_width_pct``/``StrategyResult``/``ScannerCandidate`` value is produced by the real
pipeline from per-symbol previous-session candles; nothing is injected. The width identity is
``H + L + C == 300 => pivot == 100 => cpr_width_pct == abs(close - 100)``: BBB close=104
(width 4, narrowest), CCC close=112 (width 12), AAA close=124 (width 24, widest).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from httpx import ASGITransport, AsyncClient, Response

import app.services.market_runtime as market_runtime_module
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

# 2026-08-06 06:00 UTC == 11:30 IST on a Thursday: mid regular session, so a live tick
# classifies as LIVE_SESSION and warmup resolves the previous session (2026-08-05).
_REFERENCE = datetime(2026, 8, 6, 6, 0, tzinfo=UTC)
_TRADING_DATE = date(2026, 8, 6)
_DB = "postgresql+asyncpg://user:pass@localhost:5432/apexscan"
_REDIS = "redis://localhost:6379/0"
_ERROR_THRESHOLD = 3

_UNIVERSE = ("AAA", "BBB", "CCC")
# Per-symbol previous-session close controlling CPR width (H+L+C=300 => width=|close-100|).
_CLOSES = {"AAA": Decimal("124"), "BBB": Decimal("104"), "CCC": Decimal("112")}
# Exact Decimal strings the real calculator produces (widths 4/12/24).
_EXPECTED_WIDTH = {"BBB": "4.00", "CCC": "12.00", "AAA": "24.00"}
# Ascending cpr_width_pct: narrowest first.
_ASCENDING_ORDER = ["BBB", "CCC", "AAA"]


def _instrument(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


# --- fake mandatory dependencies (mirror test_runtime_lifecycle_integration) ------------ #
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


# --- offline fake provider (universe + per-symbol previous session + live stream) ------- #
class _FakeProvider:
    """A network-free provider double controlling per-symbol previous-session CPR width."""

    capabilities = frozenset()

    def __init__(
        self,
        *,
        symbols: tuple[str, ...] = _UNIVERSE,
        tick_order: tuple[str, ...] | None = None,
        fail_historical_for: frozenset[str] = frozenset(),
    ) -> None:
        self._symbols = symbols
        self._tick_order = tick_order if tick_order is not None else symbols
        self._fail_historical_for = fail_historical_for
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
        return tuple(_instrument(s) for s in self._symbols)

    def load_nse_cash_equity_live_universe(self) -> DhanCashEquityLiveUniverse:
        return DhanCashEquityLiveUniverse(
            underlyings=(),
            cash_references=tuple(
                DhanInstrumentReference(
                    instrument=_instrument(s),
                    security_id=f"SEC-{s}",
                    underlying_security_id=None,
                    exchange_segment="NSE_EQ",
                    provider_instrument_type="ES",
                )
                for s in self._symbols
            ),
            missing_underlyings=(),
            ambiguous_underlyings=(),
            symbol_mismatches=(),
        )

    async def load_historical_data(self, request: HistoricalRequest) -> HistoricalResult:
        """Return one canonical previous-session candle per symbol (or empty to fail warmup)."""
        self.historical_calls += 1
        symbol = request.instrument.symbol
        if symbol in self._fail_historical_for:
            return HistoricalResult(request=request, candles=())
        close = _CLOSES[symbol]
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
        """Yield one live tick per requested instrument (arrival order controlled), then block."""
        self.stream_calls += 1
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
    provider: _FakeProvider, *, strategies_enabled: str = "narrow_cpr"
) -> tuple[ApplicationLifecycle, LiveMarketRuntimeDependency, object]:
    """Compose via ApplicationLifecycle + LiveMarketRuntimeDependency(adapter) + create_app."""
    dependency = LiveMarketRuntimeDependency(
        settings=_provider_settings(strategies_enabled=strategies_enabled),
        error_threshold=_ERROR_THRESHOLD,
        adapter=provider,  # type: ignore[arg-type]
        clock=ManualClock(_REFERENCE),
    )
    lifecycle = ApplicationLifecycle(_FakeDatabase([]), _FakeRedis([]), provider=dependency)
    app = create_app(lifecycle=lifecycle)
    await lifecycle.start(_app_settings())
    return lifecycle, dependency, app


def _runtime(dependency: LiveMarketRuntimeDependency) -> LiveMarketRuntime:
    """Reach the composed runtime (white-box seam for pipeline-internal assertions)."""
    composition = dependency._composition  # noqa: SLF001
    assert composition is not None
    return composition.runtime


async def _wait_until(predicate: object) -> None:
    for _ in range(1000):
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not met in time")


async def _wait_eligible(dependency: LiveMarketRuntimeDependency, count: int) -> None:
    def _ready() -> bool:
        snapshot = dependency.scanner_snapshot("narrow_cpr")
        return snapshot is not None and snapshot.eligible_count == count

    await _wait_until(_ready)


async def _get(app: object, path: str) -> Response:
    transport = ASGITransport(app=app)  # ASGITransport does not run the lifespan hook
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# --------------------------------------------------------------------------- #
# Proofs 1/2/3/5/6/8 — the core pipeline: START/RUNNING, warmup, CPR, ranking, REST.
# --------------------------------------------------------------------------- #
async def test_core_pipeline_ranks_and_serves_over_rest() -> None:
    provider = _FakeProvider()
    lifecycle, dependency, app = await _start(provider)
    runtime = _runtime(dependency)
    try:
        # Proof 1/2: START ran -> the session requirement entered the effective union.
        reqs = runtime.historical_requirements.effective_requirements()
        assert HistoricalRequirement(timeframe=Timeframe.session(), lookback=1) in reqs

        await _wait_eligible(dependency, 3)  # RUNNING evaluated all three via the real pipeline
        snapshot = dependency.scanner_snapshot("narrow_cpr")
        assert snapshot is not None

        # Proof 5/8: ascending rank, rank-1 narrowest, COMPLETE (all three MATCHED).
        assert [c.instrument.symbol for c in snapshot.candidates] == _ASCENDING_ORDER
        assert [c.rank for c in snapshot.candidates] == [1, 2, 3]
        assert snapshot.expected_count == 3
        assert snapshot.eligible_count == 3
        assert snapshot.evaluated_count == 3
        assert snapshot.completeness.value == "complete"

        # Proof 2/3: CPR derived from the 2026-08-05 candle; snapshot trading_date is 2026-08-06.
        assert snapshot.trading_date == _TRADING_DATE
        for candidate in snapshot.candidates:
            assert candidate.ranking_metric_name == "cpr_width_pct"
            assert (
                str(candidate.ranking_metric_value) == _EXPECTED_WIDTH[candidate.instrument.symbol]
            )

        # Proof 6: REST GET reflects the scanner verbatim, neutral candidate shape, no-store.
        response = await _get(app, "/api/v1/scanners/narrow_cpr")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()["snapshot"]
        assert body is not None
        assert body["strategy_id"] == "narrow_cpr"
        assert body["trading_date"] == "2026-08-06"
        assert [c["symbol"] for c in body["candidates"]] == _ASCENDING_ORDER
        assert [c["rank"] for c in body["candidates"]] == [1, 2, 3]
        for candidate in body["candidates"]:
            assert candidate["ranking_metric_value"] == _EXPECTED_WIDTH[candidate["symbol"]]
            assert set(candidate) == {
                "rank",
                "exchange",
                "symbol",
                "status",
                "ranking_metric_name",
                "ranking_metric_value",
            }
            assert "security_id" not in candidate and "exchange_segment" not in candidate
            assert "score" not in candidate  # narrow_cpr score stays None
    finally:
        await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# Proof 7 — REST limit projects the top-N by rank; counts describe the full snapshot.
# --------------------------------------------------------------------------- #
async def test_rest_limit_projects_top_n_without_changing_counts() -> None:
    provider = _FakeProvider()
    lifecycle, dependency, app = await _start(provider)
    try:
        await _wait_eligible(dependency, 3)
        response = await _get(app, "/api/v1/scanners/narrow_cpr?limit=2")
        assert response.status_code == 200
        body = response.json()["snapshot"]
        assert [c["symbol"] for c in body["candidates"]] == ["BBB", "CCC"]
        assert [c["rank"] for c in body["candidates"]] == [1, 2]
        assert body["eligible_count"] == 3
        assert body["expected_count"] == 3
    finally:
        await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# Proof 4 — StrategyResultsPublished carries the narrow_cpr result + trading_date.
# --------------------------------------------------------------------------- #
async def test_strategy_results_published_event_carries_result_and_trading_date() -> None:
    provider = _FakeProvider()
    lifecycle, dependency, _app = await _start(provider)
    runtime = _runtime(dependency)
    # Safe to subscribe now: lifecycle.start returns before yielding to the ingestion task,
    # so no tick has been dispatched yet.
    recorded: list[StrategyResultsPublished] = []
    runtime.bus.subscribe(StrategyResultsPublished, recorded.append)
    try:
        await _wait_eligible(dependency, 3)
        narrow = [
            event
            for event in recorded
            if any(result.strategy_id == "narrow_cpr" for result in event.results)
        ]
        assert narrow, "expected at least one StrategyResultsPublished for narrow_cpr"
        assert all(event.trading_date == _TRADING_DATE for event in narrow)
        metrics = {
            metric.name for event in narrow for result in event.results for metric in result.metrics
        }
        assert "cpr_width_pct" in metrics
    finally:
        await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# Proof 9 — the API read path triggers no provider/historical/evaluation work.
# --------------------------------------------------------------------------- #
async def test_api_read_path_is_isolated_from_pipeline_work() -> None:
    provider = _FakeProvider()
    lifecycle, dependency, app = await _start(provider)
    runtime = _runtime(dependency)
    evaluations: list[StrategyResultsPublished] = []
    runtime.bus.subscribe(StrategyResultsPublished, evaluations.append)
    try:
        await _wait_eligible(dependency, 3)
        stream_calls = provider.stream_calls
        historical_calls = provider.historical_calls
        evaluation_count = len(evaluations)
        for _ in range(5):
            assert (await _get(app, "/api/v1/scanners/narrow_cpr")).status_code == 200
        assert provider.stream_calls == stream_calls
        assert provider.historical_calls == historical_calls
        assert len(evaluations) == evaluation_count
    finally:
        await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# Proof 10 — arrival order does not change the final ranking (determinism).
# --------------------------------------------------------------------------- #
async def _final_order(tick_order: tuple[str, ...]) -> list[str]:
    provider = _FakeProvider(tick_order=tick_order)
    lifecycle, dependency, _app = await _start(provider)
    try:
        await _wait_eligible(dependency, 3)
        snapshot = dependency.scanner_snapshot("narrow_cpr")
        assert snapshot is not None
        return [c.instrument.symbol for c in snapshot.candidates]
    finally:
        await lifecycle.shutdown()


async def test_arrival_order_does_not_change_ranking() -> None:
    first = await _final_order(("AAA", "BBB", "CCC"))
    second = await _final_order(("CCC", "AAA", "BBB"))
    assert first == second == _ASCENDING_ORDER


# --------------------------------------------------------------------------- #
# Proof 11 — two identical offline runs yield identical ranking and REST JSON.
# --------------------------------------------------------------------------- #
async def _run_rest_json() -> object:
    provider = _FakeProvider()
    lifecycle, dependency, app = await _start(provider)
    try:
        await _wait_eligible(dependency, 3)
        return (await _get(app, "/api/v1/scanners/narrow_cpr")).json()
    finally:
        await lifecycle.shutdown()


async def test_repeat_runs_are_deterministic() -> None:
    assert await _run_rest_json() == await _run_rest_json()


# --------------------------------------------------------------------------- #
# Proof 12 — a single un-warmable instrument yields an honest PARTIAL snapshot.
#
# ADR-007 partial-universe readiness (PUR2/PUR3): START readiness is infrastructure-level,
# so one un-warmable instrument no longer suppresses the whole strategy. With CCC's
# previous session un-warmable (empty candles → local shortfall), narrow_cpr reaches
# RUNNING, AAA and BBB evaluate and rank, CCC is skipped per-context (no width=0
# fabrication), and the scanner reports PARTIAL: expected_count=3, evaluated_count=2,
# eligible_count=2. The REST route serves the two real candidates. This proves the
# partial-universe behavior end-to-end and the no-fabrication guarantee (CCC is absent).
# --------------------------------------------------------------------------- #
async def test_un_warmable_instrument_yields_partial_snapshot() -> None:
    provider = _FakeProvider(fail_historical_for=frozenset({"CCC"}))
    lifecycle, dependency, app = await _start(provider)
    runtime = _runtime(dependency)
    try:
        await _wait_eligible(dependency, 2)  # AAA and BBB evaluate; CCC is skipped
        assert runtime._strategy_lifecycle.state_of("narrow_cpr") is State.RUNNING  # noqa: SLF001
        snapshot = dependency.scanner_snapshot("narrow_cpr")
        assert snapshot is not None
        assert [c.instrument.symbol for c in snapshot.candidates] == ["BBB", "AAA"]
        assert [c.rank for c in snapshot.candidates] == [1, 2]
        assert snapshot.expected_count == 3
        assert snapshot.evaluated_count == 2
        assert snapshot.eligible_count == 2
        assert snapshot.completeness.value == "partial"
        candidate_symbols = {c.instrument.symbol for c in snapshot.candidates}
        assert "CCC" not in candidate_symbols  # un-warmable instrument absent, never fabricated
        for candidate in snapshot.candidates:
            symbol = candidate.instrument.symbol
            assert str(candidate.ranking_metric_value) == _EXPECTED_WIDTH[symbol]

        response = await _get(app, "/api/v1/scanners/narrow_cpr")
        assert response.status_code == 200
        body = response.json()["snapshot"]
        assert [c["symbol"] for c in body["candidates"]] == ["BBB", "AAA"]
        assert body["expected_count"] == 3
        assert body["eligible_count"] == 2
    finally:
        await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# Proof 13 — with narrow_cpr disabled the scanner is inert and the REST route 404s.
# --------------------------------------------------------------------------- #
async def test_disabled_strategy_is_inert_and_not_scannable() -> None:
    provider = _FakeProvider()
    lifecycle, dependency, app = await _start(provider, strategies_enabled="")
    try:
        assert dependency.scanner_snapshot("narrow_cpr") is None
        assert "narrow_cpr" not in dependency.scannable_strategy_ids()
        response = await _get(app, "/api/v1/scanners/narrow_cpr")
        assert response.status_code == 404  # composed with policies only for enabled strategies
        assert response.headers["cache-control"] == "no-store"
    finally:
        await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# Proof 14 — an unknown strategy id 404s and mutates no runtime state.
# --------------------------------------------------------------------------- #
async def test_unknown_strategy_is_404_without_mutation() -> None:
    provider = _FakeProvider()
    lifecycle, dependency, app = await _start(provider)
    try:
        await _wait_eligible(dependency, 3)
        response = await _get(app, "/api/v1/scanners/does_not_exist")
        assert response.status_code == 404
        # narrow_cpr snapshot is untouched by the unknown lookup.
        snapshot = dependency.scanner_snapshot("narrow_cpr")
        assert snapshot is not None and snapshot.eligible_count == 3
    finally:
        await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# Proof 15 — governed shutdown: provider disconnected, ingestion stopped, no orphan tasks.
# --------------------------------------------------------------------------- #
async def test_clean_shutdown_stops_ingestion_and_disconnects_provider() -> None:
    provider = _FakeProvider()
    lifecycle, dependency, _app = await _start(provider)
    runtime = _runtime(dependency)
    await _wait_eligible(dependency, 3)
    await lifecycle.shutdown()
    assert provider.disconnected.is_set()
    status = runtime.status()
    assert status.state is RuntimeState.SHUTDOWN
    assert not status.ingestion_running
    assert not status.refresh_driver_running
    assert not dependency.scanner_read_available()


# --------------------------------------------------------------------------- #
# Proof 16 — the runtime owns exactly five managed asyncio tasks (the ingestion supervisor,
# refresh driver, calendar monitor, the ADR-015 evidence-observer driver, and the SECTOR-VIEW-1B
# sector-shadow evaluator — the last two gated by their default-OFF flags); the E2E adds none.
# The ingestion supervisor (MARKET-INGESTION-RESILIENCE-1) runs its single stream inline, so it
# is still exactly one managed task, not two.
# --------------------------------------------------------------------------- #
def test_market_runtime_creates_exactly_five_tasks() -> None:
    source = Path(market_runtime_module.__file__).read_text()
    assert source.count("asyncio.create_task") == 5
