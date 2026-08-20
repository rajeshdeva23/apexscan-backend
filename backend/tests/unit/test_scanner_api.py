"""Generic cross-instrument scanner REST endpoint tests (ADR-012 REST addendum)."""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient, Response

import app.api.v1.endpoints.scanners as scanners_module
import app.schemas.scanner as scanner_schema_module
from app.core.lifecycle import ApplicationLifecycle
from app.events.bus import EventBus
from app.main import create_app
from app.schemas.market_data import Instrument, ProviderHealth, ProviderStatus
from app.services.cross_instrument_scanner import (
    CrossInstrumentStrategyScanner,
    ScannerOrdering,
    ScannerRankingPolicy,
    ScannerRankingPolicyRegistry,
)
from app.strategies.enums import EvaluationStatus
from app.strategies.results import MetricEntry, StrategyResult
from app.strategy_manager.events import StrategyResultsPublished

_TD = date(2026, 8, 6)
_NOW = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)
_CPR = ScannerRankingPolicy("narrow_cpr", "cpr_width_pct", ScannerOrdering.ASCENDING)
_MOM = ScannerRankingPolicy("fake_momentum", "momentum_strength", ScannerOrdering.DESCENDING)
_DIRECTIONAL = {"direction", "bias", "side", "long", "short", "buy", "sell", "bullish", "bearish"}


class _HealthyDatabase:
    def __init__(self) -> None:
        self.initialize = AsyncMock()
        self.verify_connectivity = AsyncMock()
        self.dispose = AsyncMock()


class _HealthyRedis:
    def __init__(self) -> None:
        self.initialize = AsyncMock()
        self.verify_connectivity = AsyncMock()
        self.close = AsyncMock()


class _ScannerSource:
    """A ProviderDependency that also exposes the read-only ScannerSnapshotSource seam."""

    def __init__(self, scanner: CrossInstrumentStrategyScanner, *, available: bool = True) -> None:
        self._scanner = scanner
        self._available = available

    async def start(self, timeout_seconds: float) -> None:
        return None

    async def verify_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.UNKNOWN, observed_at=_NOW)

    async def shutdown(self) -> None:
        return None

    def scanner_read_available(self) -> bool:
        return self._available

    def scannable_strategy_ids(self) -> tuple[str, ...]:
        return self._scanner.scannable_strategy_ids()

    def scanner_snapshot(self, strategy_id: str):  # noqa: ANN201 (returns ScannerSnapshot | None)
        return self._scanner.snapshot(strategy_id)


def _instrument(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _scanner(
    symbols: tuple[str, ...], policies: tuple[ScannerRankingPolicy, ...] = (_CPR,)
) -> tuple[CrossInstrumentStrategyScanner, EventBus]:
    bus = EventBus()
    scanner = CrossInstrumentStrategyScanner(
        instruments=tuple(_instrument(symbol) for symbol in symbols),
        policies=ScannerRankingPolicyRegistry(policies),
        bus=bus,
    )
    scanner.subscribe()
    return scanner, bus


def _publish(
    bus: EventBus,
    symbol: str,
    value: str,
    *,
    strategy_id: str = "narrow_cpr",
    metric: str = "cpr_width_pct",
    status: EvaluationStatus = EvaluationStatus.MATCHED,
) -> None:
    instrument = _instrument(symbol)
    result = StrategyResult(
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        config_version="1.0.0",
        instrument=instrument,
        context_version=1,
        evaluation_timestamp=_NOW,
        status=status,
        reason_codes=("X",) if status is EvaluationStatus.MATCHED else (),
        metrics=(MetricEntry(name=metric, value=Decimal(value)),),
    )
    bus.publish(
        StrategyResultsPublished(
            instrument=instrument, context_version=1, results=(result,), ranked=(), trading_date=_TD
        )
    )


def _app(source: _ScannerSource | None) -> object:
    lifecycle = ApplicationLifecycle(_HealthyDatabase(), _HealthyRedis(), provider=source)
    return create_app(lifecycle=lifecycle)


async def _get(app: object, path: str) -> Response:
    transport = ASGITransport(app=app)  # ASGITransport does not run the lifespan hook
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# A/B/C/T — known strategy + snapshot → 200, rank order preserved (narrowest = rank 1).
async def test_known_strategy_returns_ranked_snapshot() -> None:
    scanner, bus = _scanner(("AAA", "BBB"))
    _publish(bus, "AAA", "0.03")
    _publish(bus, "BBB", "0.01")
    response = await _get(_app(_ScannerSource(scanner)), "/api/v1/scanners/narrow_cpr")
    assert response.status_code == 200
    body = response.json()["snapshot"]
    assert body["strategy_id"] == "narrow_cpr"
    assert body["trading_date"] == "2026-08-06"
    assert [c["symbol"] for c in body["candidates"]] == ["BBB", "AAA"]
    assert [c["rank"] for c in body["candidates"]] == [1, 2]
    assert response.headers["cache-control"] == "no-store"


# D — Decimal serialized as an exact string (no binary float).
async def test_decimal_metric_is_exact_string() -> None:
    scanner, bus = _scanner(("AAA",))
    _publish(bus, "AAA", "0.03125")
    response = await _get(_app(_ScannerSource(scanner)), "/api/v1/scanners/narrow_cpr")
    candidate = response.json()["snapshot"]["candidates"][0]
    assert candidate["ranking_metric_value"] == "0.03125"
    assert isinstance(candidate["ranking_metric_value"], str)


# E — PARTIAL exposed verbatim.
async def test_partial_completeness_exposed() -> None:
    scanner, bus = _scanner(("AAA", "BBB", "CCC"))
    _publish(bus, "AAA", "0.01")
    response = await _get(_app(_ScannerSource(scanner)), "/api/v1/scanners/narrow_cpr")
    assert response.json()["snapshot"]["completeness"] == "partial"


# F — COMPLETE exposed verbatim.
async def test_complete_completeness_exposed() -> None:
    scanner, bus = _scanner(("AAA", "BBB"))
    _publish(bus, "AAA", "0.01")
    _publish(bus, "BBB", "0.02")
    response = await _get(_app(_ScannerSource(scanner)), "/api/v1/scanners/narrow_cpr")
    assert response.json()["snapshot"]["completeness"] == "complete"


# G — limit projects the top-N by rank; counts describe the full snapshot.
async def test_limit_projects_candidates_without_changing_counts() -> None:
    scanner, bus = _scanner(("AAA", "BBB", "CCC"))
    for symbol, width in (("AAA", "0.03"), ("BBB", "0.01"), ("CCC", "0.02")):
        _publish(bus, symbol, width)
    response = await _get(_app(_ScannerSource(scanner)), "/api/v1/scanners/narrow_cpr?limit=2")
    body = response.json()["snapshot"]
    assert [c["symbol"] for c in body["candidates"]] == ["BBB", "CCC"]
    assert body["eligible_count"] == 3  # counts still describe the full snapshot
    assert body["expected_count"] == 3


# H/I — invalid limit → 422.
async def test_limit_below_one_is_422() -> None:
    scanner, bus = _scanner(("AAA",))
    _publish(bus, "AAA", "0.01")
    response = await _get(_app(_ScannerSource(scanner)), "/api/v1/scanners/narrow_cpr?limit=0")
    assert response.status_code == 422


async def test_limit_above_max_is_422() -> None:
    scanner, bus = _scanner(("AAA",))
    _publish(bus, "AAA", "0.01")
    response = await _get(_app(_ScannerSource(scanner)), "/api/v1/scanners/narrow_cpr?limit=501")
    assert response.status_code == 422


# J — known strategy, no snapshot yet → 200 with snapshot null.
async def test_known_strategy_without_snapshot_returns_null() -> None:
    scanner, _ = _scanner(("AAA",))  # policy present, no results fed
    response = await _get(_app(_ScannerSource(scanner)), "/api/v1/scanners/narrow_cpr")
    assert response.status_code == 200
    assert response.json() == {"snapshot": None}
    assert response.headers["cache-control"] == "no-store"


# K — unknown / non-scanner-enabled strategy → 404.
async def test_unknown_strategy_is_404() -> None:
    scanner, _ = _scanner(("AAA",))
    response = await _get(_app(_ScannerSource(scanner)), "/api/v1/scanners/ghost")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


# L — provider disabled / no scanner source → 503.
async def test_provider_disabled_is_503() -> None:
    response = await _get(_app(None), "/api/v1/scanners/narrow_cpr")
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


# M — runtime present but not available (not started/failed) → 503.
async def test_runtime_unavailable_is_503() -> None:
    scanner, bus = _scanner(("AAA",))
    _publish(bus, "AAA", "0.01")
    response = await _get(
        _app(_ScannerSource(scanner, available=False)), "/api/v1/scanners/narrow_cpr"
    )
    assert response.status_code == 503


# V/W — a future strategy uses the same endpoint (descending), no narrow_cpr branch.
async def test_future_strategy_reuses_same_endpoint() -> None:
    scanner, bus = _scanner(("AAA", "BBB", "CCC"), policies=(_CPR, _MOM))
    for symbol, strength in (("AAA", "10"), ("BBB", "30"), ("CCC", "20")):
        _publish(bus, symbol, strength, strategy_id="fake_momentum", metric="momentum_strength")
    response = await _get(_app(_ScannerSource(scanner)), "/api/v1/scanners/fake_momentum")
    assert response.status_code == 200
    assert [c["symbol"] for c in response.json()["snapshot"]["candidates"]] == ["BBB", "CCC", "AAA"]


# R/S/X — canonical Instrument only; no provider ids; no directional fields.
async def test_response_is_neutral_and_canonical() -> None:
    scanner, bus = _scanner(("AAA",))
    _publish(bus, "AAA", "0.01")
    response = await _get(_app(_ScannerSource(scanner)), "/api/v1/scanners/narrow_cpr")
    candidate = response.json()["snapshot"]["candidates"][0]
    assert set(candidate) == {
        "rank",
        "exchange",
        "symbol",
        "status",
        "ranking_metric_name",
        "ranking_metric_value",
    }
    assert not (set(candidate) & _DIRECTIONAL)
    assert "security_id" not in candidate and "exchange_segment" not in candidate


# Z — repeated GET over unchanged state is deterministic.
async def test_repeated_get_is_deterministic() -> None:
    scanner, bus = _scanner(("AAA", "BBB"))
    _publish(bus, "AAA", "0.03")
    _publish(bus, "BBB", "0.01")
    app = _app(_ScannerSource(scanner))
    first = await _get(app, "/api/v1/scanners/narrow_cpr")
    second = await _get(app, "/api/v1/scanners/narrow_cpr")
    assert first.json() == second.json()


# Provider-neutral endpoint/schema modules.
def test_scanner_api_modules_are_provider_neutral() -> None:
    for module in (scanners_module, scanner_schema_module):
        source = inspect.getsource(module).lower()
        for forbidden in ("dhan", "security_id", "exchange_segment", "httpx"):
            assert forbidden not in source
