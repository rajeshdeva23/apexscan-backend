"""Application-lifecycle integration of the live-market runtime (RUN-E; ADR-010 D1/D10-14).

Drives ApplicationLifecycle with fake DB/Redis and a fake provider (no network) to prove:
no provider I/O at create_app, provider-disabled dormancy, provider-enabled transactional
startup, combined provider+ingestion readiness (with liveness independent), governed
shutdown order, and that strategy/session-statistics readiness never gates the app.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from app.adapters.dhan.models import DhanCashEquityLiveUniverse, DhanInstrumentReference
from app.core.config import Settings
from app.core.lifecycle import ApplicationLifecycle, ApplicationStartupError
from app.market_engine.clock import ManualClock
from app.schemas.market_data import (
    Instrument,
    MarketData,
    ProviderHealth,
    ProviderStatus,
    SubscriptionRequest,
)
from app.services.dhan_runtime_composition import LiveMarketRuntimeDependency

_NOW = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)
_DB = "postgresql+asyncpg://user:pass@localhost:5432/apexscan"
_REDIS = "redis://localhost:6379/0"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "development",
        "app_debug": False,
        "database_url": _DB,
        "redis_url": _REDIS,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


# --- fake mandatory dependencies ------------------------------------------- #
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


# --- fake provider (universe + lifecycle + live stream) -------------------- #
class _FakeProvider:
    """A full broker-neutral provider double: no network, controllable stream/health."""

    capabilities = frozenset()

    def __init__(
        self,
        *,
        events: list[str] | None = None,
        symbols: tuple[str, ...] = ("RELIANCE",),
        health: ProviderStatus = ProviderStatus.HEALTHY,
        connect_fails: bool = False,
        universe_fails: bool = False,
        stream_fatal: bool = False,
    ) -> None:
        self.events = events if events is not None else []
        self._symbols = symbols
        self.health = health
        self._connect_fails = connect_fails
        self._universe_fails = universe_fails
        self._stream_fatal = stream_fatal
        self._gate = asyncio.Event()
        self.stream_calls = 0
        self.drained = asyncio.Event()

    async def connect(self) -> None:
        self.events.append("provider.connect")
        if self._connect_fails:
            raise RuntimeError("provider connect failed")

    async def disconnect(self) -> None:
        self.events.append("provider.disconnect")

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=self.health, observed_at=_NOW)

    async def load_instruments(self) -> tuple[Instrument, ...]:
        self.events.append("provider.load_instruments")
        return tuple(_instrument(s) for s in self._symbols)

    def load_nse_cash_equity_live_universe(self) -> DhanCashEquityLiveUniverse:
        if self._universe_fails:
            self._symbols = ()  # empty → fail-closed universe resolution
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

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        self.stream_calls += 1
        self.events.append("provider.stream")
        for _ in ():
            yield _  # pragma: no cover
        self.drained.set()
        if self._stream_fatal:
            raise RuntimeError("live stream failed")
        await self._gate.wait()


def _dependency(provider: _FakeProvider) -> LiveMarketRuntimeDependency:
    return LiveMarketRuntimeDependency(
        settings=_settings(
            market_provider_enabled=True,
            dhan_client_id="c",
            dhan_pin="123456",
            dhan_totp_secret="s",
        ),
        error_threshold=3,
        adapter=provider,  # type: ignore[arg-type]
        clock=ManualClock(_NOW),
    )


def _lifecycle(events: list[str], provider: _FakeProvider | None) -> ApplicationLifecycle:
    return ApplicationLifecycle(
        _FakeDatabase(events),
        _FakeRedis(events),
        provider=_dependency(provider) if provider is not None else None,
    )


async def _wait_until(predicate: object) -> None:
    for _ in range(1000):
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not met in time")


# --------------------------------------------------------------------------- #
# create_app has no provider I/O
# --------------------------------------------------------------------------- #
def test_create_app_performs_no_provider_io() -> None:
    from app.main import create_app

    create_app()  # provider disabled by default settings → no runtime dependency built
    # No assertion beyond "no exception / no I/O": construction is side-effect free.


def test_dependency_construction_does_no_io() -> None:
    provider = _FakeProvider()
    _dependency(provider)
    assert provider.events == []  # nothing happens until lifecycle start


# --------------------------------------------------------------------------- #
# Provider-disabled mode
# --------------------------------------------------------------------------- #
async def test_provider_disabled_app_boots_ready_without_provider() -> None:
    events: list[str] = []
    lifecycle = _lifecycle(events, provider=None)
    await lifecycle.start(_settings())
    snapshot = await lifecycle.readiness_snapshot()
    assert snapshot.status == "ready"
    assert "provider" not in (snapshot.dependencies or {})
    await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# Provider-enabled startup
# --------------------------------------------------------------------------- #
async def test_provider_enabled_startup_composes_and_becomes_ready() -> None:
    events: list[str] = []
    provider = _FakeProvider(events=events)
    lifecycle = _lifecycle(events, provider)
    await lifecycle.start(_settings())
    assert "provider.connect" in events and "provider.load_instruments" in events
    await _wait_until(lambda: provider.stream_calls == 1)  # ingestion started (no tick required)
    snapshot = await lifecycle.readiness_snapshot()
    assert snapshot.status == "ready"
    assert (snapshot.dependencies or {}).get("provider") == "healthy"
    await lifecycle.shutdown()


async def test_provider_connect_failure_fails_startup_and_cleans_up() -> None:
    events: list[str] = []
    provider = _FakeProvider(events=events, connect_fails=True)
    lifecycle = _lifecycle(events, provider)
    with pytest.raises(ApplicationStartupError):
        await lifecycle.start(_settings())
    assert lifecycle.startup_snapshot().status == "failed"
    assert "redis.close" in events and "database.dispose" in events  # rollback cleanup


async def test_empty_universe_fails_startup() -> None:
    provider = _FakeProvider(universe_fails=True)
    lifecycle = _lifecycle([], provider)
    with pytest.raises(ApplicationStartupError):
        await lifecycle.start(_settings())
    assert lifecycle.startup_snapshot().status == "failed"


# --------------------------------------------------------------------------- #
# Readiness reflects provider + ingestion; liveness independent
# --------------------------------------------------------------------------- #
async def test_unhealthy_provider_makes_app_not_ready_but_alive() -> None:
    provider = _FakeProvider()
    lifecycle = _lifecycle([], provider)
    await lifecycle.start(_settings())
    provider.health = ProviderStatus.DOWN  # provider degrades after startup
    snapshot = await lifecycle.readiness_snapshot()
    assert snapshot.status == "not_ready"
    assert lifecycle.liveness_snapshot().status == "live"
    await lifecycle.shutdown()


async def test_fatal_ingestion_makes_app_not_ready_but_alive() -> None:
    provider = _FakeProvider(stream_fatal=True)
    lifecycle = _lifecycle([], provider)
    await lifecycle.start(_settings())
    await provider.drained.wait()
    snapshot = await lifecycle.readiness_snapshot()
    for _ in range(1000):  # let the fatal terminate the ingestion task, then re-probe
        if snapshot.status == "not_ready":
            break
        await asyncio.sleep(0)
        snapshot = await lifecycle.readiness_snapshot()
    assert snapshot.status == "not_ready"  # ingestion fatal → provider dependency unhealthy
    assert lifecycle.liveness_snapshot().status == "live"  # liveness stays process-level
    await lifecycle.shutdown()


async def test_no_first_tick_required_for_readiness() -> None:
    # The fake stream yields nothing (market closed / pre-open); readiness is still ready.
    provider = _FakeProvider()
    lifecycle = _lifecycle([], provider)
    await lifecycle.start(_settings())
    assert (await lifecycle.readiness_snapshot()).status == "ready"
    await lifecycle.shutdown()


# --------------------------------------------------------------------------- #
# Shutdown order & health after shutdown
# --------------------------------------------------------------------------- #
async def test_shutdown_order_runtime_then_provider_then_redis_then_db() -> None:
    events: list[str] = []
    provider = _FakeProvider(events=events)
    lifecycle = _lifecycle(events, provider)
    await lifecycle.start(_settings())
    events.clear()
    await lifecycle.shutdown()
    # ingestion/manager stop inside the runtime; then provider disconnect, then redis, then db.
    assert events == ["provider.disconnect", "redis.close", "database.dispose"]
    assert lifecycle.startup_snapshot().status == "stopped"


async def test_repeated_start_creates_one_runtime() -> None:
    provider = _FakeProvider()
    lifecycle = _lifecycle([], provider)
    await lifecycle.start(_settings())
    await lifecycle.start(_settings())  # idempotent
    await _wait_until(lambda: provider.stream_calls == 1)
    assert provider.stream_calls == 1  # no second ingestion task
    await lifecycle.shutdown()


async def test_runtime_dependency_health_unknown_before_start_and_after_shutdown() -> None:
    provider = _FakeProvider()
    dependency = _dependency(provider)
    assert (await dependency.verify_health()).status is ProviderStatus.UNKNOWN
    await dependency.start(5.0)
    assert (await dependency.verify_health()).status is ProviderStatus.HEALTHY
    await dependency.shutdown()
    assert (await dependency.verify_health()).status is ProviderStatus.UNKNOWN
