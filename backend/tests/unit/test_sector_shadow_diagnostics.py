"""Read-only sector-shadow diagnostics projection + endpoint (SECTOR-VIEW-1D)."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient, Response

from app.adapters.base.live_feed_diagnostics import LiveFeedDecodeDiagnostics
from app.core.lifecycle import ApplicationLifecycle
from app.events.bus import EventBus
from app.main import create_app
from app.market_engine.clock import ManualClock
from app.market_engine.context import MarketContext, MarketState, SessionContext
from app.market_engine.events import MarketContextCreated
from app.market_intelligence.sector import MembershipResolver, load_sector_membership_dataset
from app.schemas.market_data import (
    Instrument,
    ProviderHealth,
    ProviderSessionOhlc,
    ProviderStatus,
    Tick,
)
from app.schemas.sector_shadow_diagnostics import project
from app.services.sector_intelligence import (
    SectorShadowRuntime,
    SectorShadowSnapshot,
    ShadowDiagnosticsView,
    ShadowRuntimeConfig,
)

_TD = date(2026, 9, 3)
_EVAL = datetime(2026, 9, 3, 7, 0, tzinfo=UTC)
_FRESH = datetime(2026, 9, 3, 6, 58, tzinfo=UTC)
_RESOLVER = MembershipResolver(load_sector_membership_dataset())
_UNIVERSE = tuple(
    identity
    for sector_id in _RESOLVER.all_primary_sectors()
    for identity in _RESOLVER.members_of_primary_sector(sector_id)
)


def _context(
    identity: str,
    *,
    prev: Decimal | None = Decimal("100"),
    session_open: Decimal | None = Decimal("101"),
) -> MarketContext:
    exchange, symbol = identity.split(":")
    instrument = Instrument(exchange=exchange, symbol=symbol)
    ohlc = (
        ProviderSessionOhlc(
            open_price=session_open,
            high_price=max(session_open, Decimal("105")) + Decimal("1"),
            low_price=min(session_open, Decimal("105")) - Decimal("1"),
            close_price=Decimal("105"),
        )
        if session_open is not None
        else None
    )
    tick = Tick(
        instrument=instrument,
        event_timestamp=_FRESH,
        last_price=Decimal("105"),
        traded_quantity=1,
        session_ohlc=ohlc,
    )
    session = SessionContext(
        trading_date=_TD, market_state=MarketState.LIVE_SESSION, exchange_timezone="Asia/Kolkata"
    )
    return MarketContext.initial(
        instrument,
        sequence=1,
        event_timestamp=_FRESH,
        observed_at=_FRESH,
        latest_tick=tick,
        session=session,
        previous_close=prev,
    )


def _runtime_with(*, complete: int, missing_prev: int, missing_open: int) -> SectorShadowRuntime:
    bus = EventBus()
    runtime = SectorShadowRuntime(
        bus=bus,
        resolver=_RESOLVER,
        config=ShadowRuntimeConfig(interval_seconds=60),
        clock=ManualClock(_EVAL),
    )
    runtime.subscribe()
    picks = _UNIVERSE[: complete + missing_prev + missing_open]
    idx = 0
    for _ in range(complete):
        bus.publish(MarketContextCreated(context=_context(picks[idx])))
        idx += 1
    for _ in range(missing_prev):
        bus.publish(MarketContextCreated(context=_context(picks[idx], prev=None)))
        idx += 1
    for _ in range(missing_open):
        bus.publish(MarketContextCreated(context=_context(picks[idx], session_open=None)))
        idx += 1
    return runtime


def _snapshot(
    *, complete: int = 5, missing_prev: int = 0, missing_open: int = 0
) -> tuple[SectorShadowSnapshot, ShadowDiagnosticsView]:
    """Synchronous snapshot builder for non-async tests."""
    runtime = _runtime_with(complete=complete, missing_prev=missing_prev, missing_open=missing_open)
    snap = asyncio.run(runtime.evaluate_once())
    assert snap is not None
    return snap, runtime.diagnostics()


async def _snapshot_async(
    *, complete: int = 5, missing_prev: int = 0, missing_open: int = 0
) -> tuple[SectorShadowSnapshot, ShadowDiagnosticsView]:
    """Snapshot builder for async (endpoint) tests — awaits within the running loop."""
    runtime = _runtime_with(complete=complete, missing_prev=missing_prev, missing_open=missing_open)
    snap = await runtime.evaluate_once()
    assert snap is not None
    return snap, runtime.diagnostics()


# --------------------------------------------------------------------------- #
# project() — pure, no HTTP
# --------------------------------------------------------------------------- #
def test_project_disabled_state() -> None:
    response = project(snapshot=None, diagnostics=None, live_feed=None)
    assert response.enabled is False
    assert response.sectors == []
    assert response.universe is None


def test_project_no_snapshot_but_enabled() -> None:
    diagnostics = ShadowDiagnosticsView(
        events_received=0,
        events_accepted=0,
        events_rejected=0,
        unknown_instruments=0,
        duplicate_events=0,
        out_of_order_events=0,
        late_trading_date_events=0,
        rollovers=0,
        snapshot_attempts=0,
        snapshot_successes=0,
        snapshot_failures=0,
        evaluation_overruns=0,
        last_evaluation_duration_ms=None,
        last_success_timestamp=None,
    )
    response = project(snapshot=None, diagnostics=diagnostics, live_feed=None)
    assert response.enabled is True
    assert response.universe is None


def test_project_coverage_and_summary() -> None:
    snap, diag = _snapshot(complete=5)
    response = project(snapshot=snap, diagnostics=diag, live_feed=None)
    assert response.enabled is True
    assert response.trading_date == "2026-09-03"
    assert response.universe["expected_instruments"] == len(_UNIVERSE)
    assert response.universe["observed_instruments"] == 5
    assert response.universe["complete_instruments"] == 5
    assert response.universe["previous_close_count"] == 5
    assert response.coverage["observed_coverage_ratio"] is not None
    assert response.sector_summary["sector_count"] == 18
    assert len(response.sectors) == 18


def test_project_missing_previous_close_counts() -> None:
    snap, diag = _snapshot(complete=3, missing_prev=2)
    response = project(snapshot=snap, diagnostics=diag, live_feed=None)
    assert response.universe["observed_instruments"] == 5
    assert response.universe["previous_close_count"] == 3
    assert response.universe["missing_previous_close_count"] == 2
    assert response.universe["complete_instruments"] == 3


def test_project_stock_participation_bounded_and_filtered() -> None:
    snap, diag = _snapshot(complete=20)
    unfiltered = project(snapshot=snap, diagnostics=diag, live_feed=None, limit=3)
    assert len(unfiltered.stock_participation) <= 3
    some_sector = snap.stock_rankings[0].sector_id
    filtered = project(snapshot=snap, diagnostics=diag, live_feed=None, sector=some_sector)
    assert all(row["sector_id"] == some_sector for row in filtered.stock_participation)


def test_project_live_feed_dump() -> None:
    live = LiveFeedDecodeDiagnostics(
        frames_total=100,
        decoded_total=60,
        failures_total=40,
        unknown_reference_failures=40,
        previous_close_frames_seen=7,
        frames_by_response_code={4: 60, 2: 40},
    )
    response = project(snapshot=None, diagnostics=None, live_feed=live)
    assert response.live_feed["frames_total"] == 100
    assert response.live_feed["previous_close_frames_seen"] == 7
    assert response.live_feed["frames_by_response_code"] == {"4": 60, "2": 40}


# --------------------------------------------------------------------------- #
# Endpoint — read-only, via a fake ProviderDependency + SectorShadowDiagnosticsSource
# --------------------------------------------------------------------------- #
class _HealthyDep:
    def __init__(self) -> None:
        self.initialize = AsyncMock()
        self.verify_connectivity = AsyncMock()
        self.dispose = AsyncMock()
        self.close = AsyncMock()


class _ShadowSource:
    def __init__(self, snapshot, diagnostics, live_feed, *, available: bool = True) -> None:
        self._snapshot = snapshot
        self._diagnostics = diagnostics
        self._live_feed = live_feed
        self._available = available

    async def start(self, timeout_seconds: float) -> None:
        return None

    async def verify_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.UNKNOWN, observed_at=_EVAL)

    async def shutdown(self) -> None:
        return None

    def sector_shadow_read_available(self) -> bool:
        return self._available

    def sector_shadow_snapshot(self):  # noqa: ANN201
        return self._snapshot

    def sector_shadow_diagnostics(self):  # noqa: ANN201
        return self._diagnostics

    def live_feed_decode_diagnostics(self):  # noqa: ANN201
        return self._live_feed


def _app(source: _ShadowSource | None) -> object:
    lifecycle = ApplicationLifecycle(_HealthyDep(), _HealthyDep(), provider=source)
    return create_app(lifecycle=lifecycle)


async def _get(app: object, path: str) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def test_endpoint_returns_diagnostics_and_no_store() -> None:
    snap, diag = await _snapshot_async(complete=5)
    app = _app(_ShadowSource(snap, diag, None))
    resp = await _get(app, "/api/v1/diagnostics/sector-shadow")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["enabled"] is True
    assert body["sector_summary"]["sector_count"] == 18


async def test_endpoint_503_when_unavailable() -> None:
    app = _app(_ShadowSource(None, None, None, available=False))
    resp = await _get(app, "/api/v1/diagnostics/sector-shadow")
    assert resp.status_code == 503


async def test_endpoint_503_when_no_source() -> None:
    app = _app(None)
    resp = await _get(app, "/api/v1/diagnostics/sector-shadow")
    assert resp.status_code == 503


async def test_endpoint_limit_over_max_is_422() -> None:
    snap, diag = await _snapshot_async(complete=5)
    app = _app(_ShadowSource(snap, diag, None))
    resp = await _get(app, "/api/v1/diagnostics/sector-shadow?limit=999")
    assert resp.status_code == 422


async def test_endpoint_is_read_only_get_only() -> None:
    snap, diag = await _snapshot_async(complete=5)
    app = _app(_ShadowSource(snap, diag, None))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/api/v1/diagnostics/sector-shadow")).status_code == 405
        assert (await client.delete("/api/v1/diagnostics/sector-shadow")).status_code == 405


def test_project_missing_session_open_counts() -> None:
    snap, diag = _snapshot(complete=3, missing_open=2)
    response = project(snapshot=snap, diagnostics=diag, live_feed=None)
    assert response.universe["session_open_count"] == 3
    assert response.universe["missing_session_open_count"] == 2


def test_project_is_deterministic_and_triggers_no_evaluation() -> None:
    snap, diag = _snapshot(complete=5)
    first = project(snapshot=snap, diagnostics=diag, live_feed=None)
    second = project(snapshot=snap, diagnostics=diag, live_feed=None)
    assert first.model_dump() == second.model_dump()  # pure read, repeatable


def test_response_contains_no_secret_fields() -> None:
    snap, diag = _snapshot(complete=5)
    body = project(snapshot=snap, diagnostics=diag, live_feed=None).model_dump_json().lower()
    for secret in ("token", "totp", "pin", "secret", "password", "authorization", "bearer"):
        assert secret not in body


def test_diagnostics_layer_does_not_import_concrete_dhan_adapter() -> None:
    import app.api.v1.endpoints.diagnostics as endpoint
    import app.schemas.sector_shadow_diagnostics as schema

    for module in (endpoint, schema):
        source = __import__("inspect").getsource(module)
        assert "adapters.dhan" not in source  # depends on base seam, never a concrete adapter
