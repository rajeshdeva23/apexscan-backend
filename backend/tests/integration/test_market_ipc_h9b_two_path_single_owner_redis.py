"""Two-path single-Dhan-owner invariant across cutover / rollback / crash / loss (DECOUPLING H9B).

The load-bearing H9B proof: the LEGACY backend path (real ``compose_market_runtime``) and the
DECOUPLED ingestion service (real ``MarketIngestionService`` + ``ProviderSupervisor``) compete for
ONE fenced-lease authority (a shared ``redislite`` server, identical ``OwnershipLeaseConfig`` keys)
with fake providers. A shared owner tracker asserts CONTINUOUSLY that the number of connected
providers never exceeds one — a deliberate 0-owner gap is allowed, a 2-owner overlap is not.

Proven offline (no Dhan, no production, no authority switch):
  * acquire-before-connect on BOTH paths; a contender that loses the lease mints/connects nothing;
  * cutover legacy→decoupled and rollback decoupled→legacy each keep peak owners == 1, fence rising;
  * crash/TTL takeover: a crashed owner's lease blocks a successor until it expires, then the
    successor acquires a strictly higher fence; the stale owner cannot return;
  * lease loss while running: the decoupled owner fails closed, disconnects, and does NOT reconnect.

Skips cleanly without redislite.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from redis.asyncio import Redis

from app.adapters.base.broker_adapter import BrokerAdapter
from app.adapters.dhan.models import DhanCashEquityLiveUniverse, DhanInstrumentReference
from app.core.config import Settings
from app.market_engine.clock import ManualClock
from app.market_engine.sequence import MonotonicSequence
from app.market_ingestion.mode import PhaseHFlags
from app.market_ingestion.ownership import (
    OwnerRole,
    OwnershipLeaseConfig,
    RedisOwnershipCoordinator,
)
from app.market_ingestion.ownership_runtime import (
    OwnershipAcquisitionError,
    ProviderOwnershipGuard,
)
from app.market_ingestion.publication import PublicationStack, build_publication_stack
from app.market_ingestion.service import MarketIngestionService, ServiceStatus
from app.market_ipc import MarketIpcConfig
from app.schemas.market_data import (
    Instrument,
    MarketData,
    MarketDataKind,
    ProviderHealth,
    ProviderStatus,
    SubscriptionRequest,
)
from app.services.dhan_runtime_composition import RuntimeComposition, compose_market_runtime

redislite = pytest.importorskip("redislite", reason="disposable real Redis unavailable")

_NOW = datetime(2026, 9, 9, 10, 15, 30, tzinfo=UTC)
_TD = date(2026, 9, 9)
_PRODUCER = "market-ingestion"
_DB = "postgresql+asyncpg://user:pass@localhost:5432/apexscan"
_REDIS = "redis://localhost:6379/0"
_CUT = object()


@pytest.fixture(scope="module")
def redis_socket() -> str:
    server = redislite.Redis()
    try:
        yield server.socket_file
    finally:
        server.shutdown()


@pytest.fixture
async def flushed(redis_socket: str) -> AsyncIterator[None]:
    client: Redis = Redis(unix_socket_path=redis_socket)
    await client.flushall()
    await client.aclose()
    yield


# --------------------------------------------------------------------------- #
# Shared owner tracker — the continuous peak==1 assertion
# --------------------------------------------------------------------------- #
class _OwnerTracker:
    """Counts concurrently-connected providers; a second connect is an immediate failure."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.legacy_connects = 0
        self.decoupled_connects = 0

    def connect(self, who: str) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)
        if who == "legacy":
            self.legacy_connects += 1
        else:
            self.decoupled_connects += 1
        assert self.active <= 1, f"TWO provider owners active at once (via {who})"

    def disconnect(self) -> None:
        self.active = max(0, self.active - 1)


# --------------------------------------------------------------------------- #
# Ownership plumbing (both paths share one authority domain)
# --------------------------------------------------------------------------- #
def _lease_config(**overrides: object) -> OwnershipLeaseConfig:
    base: dict[str, object] = {"lease_ttl_seconds": 30, "renewal_interval_seconds": 10}
    base.update(overrides)
    return OwnershipLeaseConfig(**base)


def _guard(
    socket: str, role: OwnerRole, *, config: OwnershipLeaseConfig | None = None
) -> ProviderOwnershipGuard:
    client: Redis = Redis(unix_socket_path=socket)
    coordinator = RedisOwnershipCoordinator(client, config or _lease_config())
    return ProviderOwnershipGuard(
        coordinator=coordinator,
        role=role,
        renewal_interval_seconds=(config or _lease_config()).renewal_interval_seconds,
        redis_to_close=client,
    )


# --------------------------------------------------------------------------- #
# Legacy path (real compose_market_runtime + a recording fake adapter)
# --------------------------------------------------------------------------- #
def _reference(symbol: str) -> DhanInstrumentReference:
    return DhanInstrumentReference(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        security_id=f"SEC-{symbol}",
        underlying_security_id=None,
        exchange_segment="NSE_EQ",
        provider_instrument_type="ES",
    )


def _universe() -> DhanCashEquityLiveUniverse:
    return DhanCashEquityLiveUniverse(
        underlyings=(),
        cash_references=(_reference("RELIANCE"),),
        missing_underlyings=(),
        ambiguous_underlyings=(),
        symbol_mismatches=(),
    )


class _LegacyAdapter(BrokerAdapter):
    """A recording legacy provider double that reports connect/disconnect to the shared tracker."""

    capabilities = frozenset()

    def __init__(self, tracker: _OwnerTracker) -> None:
        self._tracker = tracker
        self._gate = asyncio.Event()
        self.connected = False

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        for event in ():
            yield event
        await self._gate.wait()

    async def connect(self) -> None:
        self._tracker.connect("legacy")
        self.connected = True

    async def disconnect(self) -> None:
        if self.connected:
            self._tracker.disconnect()
            self.connected = False

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def load_instruments(self) -> tuple[Instrument, ...]:
        return (Instrument(exchange="NSE", symbol="RELIANCE"),)

    def load_nse_cash_equity_live_universe(self) -> DhanCashEquityLiveUniverse:
        return _universe()


def _legacy_settings() -> Settings:
    return Settings(
        app_env="development",
        database_url=_DB,
        redis_url=_REDIS,
        market_provider_enabled=True,
        market_ownership_enabled=True,
        dhan_auth_mode="totp",
        dhan_client_id="client-id",
        dhan_pin="123456",
        dhan_totp_secret="totp-secret",
    )


async def _start_legacy(
    socket: str, tracker: _OwnerTracker, *, config: OwnershipLeaseConfig | None = None
) -> tuple[RuntimeComposition, _LegacyAdapter]:
    """Compose the real legacy runtime; the fenced BACKEND lease is acquired before the connect."""
    adapter = _LegacyAdapter(tracker)
    composition = await compose_market_runtime(
        settings=_legacy_settings(),
        error_threshold=3,
        adapter=adapter,
        clock=ManualClock(_NOW),
        sequence=MonotonicSequence(),
        ownership=_guard(socket, OwnerRole.BACKEND, config=config),
    )
    return composition, adapter


# --------------------------------------------------------------------------- #
# Decoupled path (real MarketIngestionService + ProviderSupervisor)
# --------------------------------------------------------------------------- #
class _DecoupledProvider(BrokerAdapter):
    """A recording decoupled provider double with a cuttable stream (single-loop, no Dhan)."""

    capabilities = frozenset()

    def __init__(self, tracker: _OwnerTracker) -> None:
        self._tracker = tracker
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self.connect_calls = 0
        self.stream_calls = 0
        self.connected = False

    async def connect(self) -> None:
        self.connect_calls += 1
        self._tracker.connect("decoupled")
        self.connected = True

    async def disconnect(self) -> None:
        if self.connected:
            self._tracker.disconnect()
            self.connected = False

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        self.stream_calls += 1
        while True:
            item = await self._queue.get()
            if item is _CUT:
                raise ConnectionError("simulated recoverable transport drop")
            yield item  # type: ignore[misc]

    def cut(self) -> None:
        self._queue.put_nowait(_CUT)


class _FixedTradingDate:
    def current_trading_date(self) -> date:
        return _TD


def _decoupled_flags() -> PhaseHFlags:
    return PhaseHFlags(
        market_ingestion_service_enabled=True,
        ipc_publisher_enabled=True,
        ipc_consumer_enabled=False,
        ipc_shadow_compare_enabled=False,
        ipc_authoritative_enabled=False,
        legacy_market_path_enabled=True,
    )


def _request() -> SubscriptionRequest:
    return SubscriptionRequest(
        instruments=(Instrument(exchange="NSE", symbol="TCS"),),
        data_types=frozenset({MarketDataKind.TICK}),
    )


def _build_decoupled(
    socket: str,
    tracker: _OwnerTracker,
    state_dir: Path,
    *,
    config: OwnershipLeaseConfig | None = None,
) -> tuple[MarketIngestionService, _DecoupledProvider, PublicationStack]:
    provider = _DecoupledProvider(tracker)
    stack = build_publication_stack(
        redis=Redis(unix_socket_path=socket),
        config=MarketIpcConfig(block_ms=0),
        producer_id=_PRODUCER,
        state_dir=state_dir,
        now=lambda: _NOW,
        trading_date_source=_FixedTradingDate(),
        universe_version=7,
    )
    service = MarketIngestionService(
        flags=_decoupled_flags(),
        provider=provider,
        subscription_request=_request(),
        publication=stack,
        ownership=_guard(socket, OwnerRole.INGESTION, config=config),
        observer_interval_seconds=0.005,
    )
    return service, provider, stack


async def _wait(predicate, message: str) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(message)


# =========================================================================== #
# §22 — token-mint / connect guard: a contender that loses ownership connects nothing
# =========================================================================== #
async def test_decoupled_blocked_when_legacy_owns_connects_nothing(
    redis_socket: str, tmp_path: Path, flushed: None
) -> None:
    tracker = _OwnerTracker()
    legacy, _adapter = await _start_legacy(redis_socket, tracker)
    service, provider, _stack = _build_decoupled(redis_socket, tracker, tmp_path)
    try:
        with pytest.raises(OwnershipAcquisitionError):
            await service.start()
        assert provider.connect_calls == 0  # never minted/connected
        assert service.status is ServiceStatus.FAILED
        assert tracker.peak == 1
    finally:
        await service.stop()
        await legacy.shutdown()


async def test_legacy_blocked_when_decoupled_owns_connects_nothing(
    redis_socket: str, tmp_path: Path, flushed: None
) -> None:
    tracker = _OwnerTracker()
    service, provider, _stack = _build_decoupled(redis_socket, tracker, tmp_path)
    await service.start()
    await _wait(lambda: provider.connect_calls == 1, "decoupled never connected")
    try:
        legacy_adapter = _LegacyAdapter(tracker)
        with pytest.raises(OwnershipAcquisitionError):
            await compose_market_runtime(
                settings=_legacy_settings(),
                error_threshold=3,
                adapter=legacy_adapter,
                clock=ManualClock(_NOW),
                sequence=MonotonicSequence(),
                ownership=_guard(redis_socket, OwnerRole.BACKEND),
            )
        assert legacy_adapter.connected is False
        assert tracker.peak == 1
    finally:
        await service.stop()


# =========================================================================== #
# §18 — cutover legacy → decoupled (peak owners == 1, fence rises)
# =========================================================================== #
async def test_cutover_legacy_to_decoupled_keeps_single_owner(
    redis_socket: str, tmp_path: Path, flushed: None
) -> None:
    tracker = _OwnerTracker()
    legacy, _adapter = await _start_legacy(redis_socket, tracker)
    assert tracker.active == 1
    legacy_fence = legacy.ownership_guard.fencing_generation

    # Overlap is refused while legacy still owns.
    blocked, _p, _s = _build_decoupled(redis_socket, tracker, tmp_path / "blocked")
    with pytest.raises(OwnershipAcquisitionError):
        await blocked.start()
    await blocked.stop()

    # Cutover: stop legacy (disconnect + release), THEN the decoupled owner acquires.
    await legacy.shutdown()
    assert tracker.active == 0  # deliberate 0-owner gap is allowed

    service, provider, _stack = _build_decoupled(redis_socket, tracker, tmp_path / "live")
    try:
        await service.start()
        await _wait(lambda: provider.connect_calls == 1, "decoupled never connected post-cutover")
        assert service.status is ServiceStatus.RUNNING
        assert tracker.active == 1
        assert tracker.peak == 1  # never two owners at once
    finally:
        await service.stop()

    assert legacy_fence == 1
    assert tracker.decoupled_connects == 1


# =========================================================================== #
# §19 — rollback decoupled → legacy (peak owners == 1, stale owner rejected)
# =========================================================================== #
async def test_rollback_decoupled_to_legacy_keeps_single_owner(
    redis_socket: str, tmp_path: Path, flushed: None
) -> None:
    tracker = _OwnerTracker()
    service, provider, _stack = _build_decoupled(redis_socket, tracker, tmp_path)
    await service.start()
    await _wait(lambda: provider.connect_calls == 1, "decoupled never connected")
    assert tracker.active == 1

    await service.stop()  # disconnect + release
    assert tracker.active == 0

    legacy, adapter = await _start_legacy(redis_socket, tracker)
    try:
        assert adapter.connected is True
        assert tracker.active == 1
        assert tracker.peak == 1
        # A fresh acquire after a prior release mints a strictly higher fence.
        assert legacy.ownership_guard.fencing_generation == 2
    finally:
        await legacy.shutdown()


# =========================================================================== #
# §20 — crash / TTL takeover: a crashed owner blocks a successor until expiry
# =========================================================================== #
async def test_crash_then_ttl_takeover_is_safe(redis_socket: str, flushed: None) -> None:
    config = _lease_config()
    crashed = _guard(redis_socket, OwnerRole.BACKEND, config=config)
    lease_a = await crashed.acquire_or_fail()
    assert lease_a.fencing_generation == 1

    # The owner "crashes": no release. A successor cannot acquire while the lease exists.
    successor = _guard(redis_socket, OwnerRole.INGESTION, config=config)
    with pytest.raises(OwnershipAcquisitionError):
        await successor.acquire_or_fail()

    # The lease TTL-expires (modelled deterministically by the key vanishing).
    evictor: Redis = Redis(unix_socket_path=redis_socket)
    await evictor.delete(config.owner_key)
    await evictor.aclose()

    lease_b = await successor.acquire_or_fail()
    assert lease_b.fencing_generation == 2  # strictly higher fence
    # The stale (crashed) owner cannot renew/validate its way back in.
    assert await crashed.validate() is False
    await successor.release()


# =========================================================================== #
# §21 — lease loss while running: fail closed, disconnect, NO reconnect
# =========================================================================== #
async def test_lease_loss_during_operation_fails_closed_no_reconnect(
    redis_socket: str, tmp_path: Path, flushed: None
) -> None:
    tracker = _OwnerTracker()
    config = _lease_config()
    service, provider, _stack = _build_decoupled(redis_socket, tracker, tmp_path, config=config)
    await service.start()
    await _wait(lambda: provider.stream_calls == 1, "decoupled stream never started")
    assert provider.connect_calls == 1

    # Externally evict the lease (a newer owner fenced it out / TTL expired), then cut the stream:
    # the supervisor's pre-reconnect ownership guard must refuse to reconnect.
    evictor: Redis = Redis(unix_socket_path=redis_socket)
    await evictor.delete(config.owner_key)
    await evictor.aclose()
    provider.cut()

    await _wait(lambda: service.terminal_failure, "service did not fail closed on lease loss")
    await _wait(lambda: provider.connected is False, "provider was not disconnected on lease loss")
    assert provider.stream_calls == 1  # NEVER reconnected
    assert tracker.active == 0
    await service.stop()


async def test_valid_reconnect_is_permitted_while_ownership_holds(
    redis_socket: str, tmp_path: Path, flushed: None
) -> None:
    tracker = _OwnerTracker()
    service, provider, _stack = _build_decoupled(redis_socket, tracker, tmp_path)
    await service.start()
    await _wait(lambda: provider.stream_calls == 1, "decoupled stream never started")

    # A recoverable drop while ownership is intact: the pre-reconnect guard validates TRUE, so the
    # supervisor reconnects (single loop) and the service keeps running as the sole owner.
    provider.cut()
    await _wait(lambda: provider.stream_calls == 2, "supervisor did not reconnect a valid owner")
    try:
        assert service.status is ServiceStatus.RUNNING
        assert service.terminal_failure is False
        assert tracker.active == 1
        assert tracker.peak == 1
    finally:
        await service.stop()
