"""Ownership→token ordering, handoff, crash, and fail-closed health (DECOUPLING PHASE H9C-P3).

Production-shaped offline proofs over a real disposable ``redislite`` server + fake Dhan:

* ordering — a guard reserves a token mint ONLY after ownership; a throttle denial / a missing
  acquire fails closed (no mint);
* handoff — backend→ingestion (and reverse) keeps at most one authorized owner, the fence strictly
  increases, and the successor cannot mint inside the cross-process cooldown;
* crash/TTL — a crashed owner's lease blocks a successor until it expires, then the cooldown still
  holds;
* health — an ownership-loss fail-close publishes a fenced non-healthy md:health so a dead
  incarnation stops advertising fresh HEALTHY (P1 LOW-1), without faking a publication break;
* diagnostic — the evidence tool refuses to open Dhan while a governed owner holds the lease;
* default — with ownership disabled nothing builds an ownership/token Redis client at all.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from redis.asyncio import Redis

from app.adapters.base.broker_adapter import BrokerAdapter
from app.market_ingestion.mode import PhaseHFlags
from app.market_ingestion.ownership import OwnerRole, OwnershipLeaseConfig
from app.market_ingestion.ownership_runtime import (
    OwnershipAcquisitionError,
    build_provider_ownership_guard,
)
from app.market_ingestion.publication import build_publication_stack
from app.market_ingestion.service import MarketIngestionService, ServiceStatus
from app.market_ingestion.token_mint_guard import TokenMintConfig, TokenMintThrottledError
from app.market_ipc import MarketIpcConfig
from app.market_ipc.health import IngestionHealthReader
from app.market_ipc.state import health_key
from app.schemas.market_data import (
    Instrument,
    MarketDataKind,
    ProviderCapability,
    ProviderHealth,
    ProviderStatus,
    SubscriptionRequest,
    Tick,
)

redislite = pytest.importorskip("redislite", reason="disposable real Redis unavailable")

_NOW = datetime(2026, 9, 17, 10, 15, 30, tzinfo=UTC)
_TD = date(2026, 9, 17)
_PRODUCER = "market-ingestion"
_OWNER_KEY = "md:provider:ownership"
_MINT_KEY = "md:provider:token:mint"


@pytest.fixture(scope="module")
def redis_socket() -> str:
    server = redislite.Redis()
    try:
        yield server.socket_file
    finally:
        server.shutdown()


@pytest.fixture
async def redis(redis_socket: str) -> Redis:
    client: Redis = Redis(unix_socket_path=redis_socket)
    await client.flushall()
    try:
        yield client
    finally:
        await client.aclose()


class _OwnSettings:
    """Minimal settings surface the ownership/token guard builder needs."""

    def __init__(
        self,
        socket: str,
        *,
        enabled: bool = True,
        ttl: int = 30,
        renewal: int = 10,
        cooldown: int = 120,
    ) -> None:
        self.redis_url = f"unix://{socket}"
        self.market_ownership_enabled = enabled
        self.market_ownership_lease_ttl_seconds = ttl
        self.market_ownership_renewal_interval_seconds = renewal
        self.market_token_mint_cooldown_seconds = cooldown

    def market_ownership_config(self) -> OwnershipLeaseConfig:
        return OwnershipLeaseConfig(
            lease_ttl_seconds=self.market_ownership_lease_ttl_seconds,
            renewal_interval_seconds=self.market_ownership_renewal_interval_seconds,
        )

    def token_mint_config(self) -> TokenMintConfig:
        return TokenMintConfig(cooldown_seconds=self.market_token_mint_cooldown_seconds)


# --------------------------------------------------------------------------- #
# PART O/P — ownership→token ordering (guard level)
# --------------------------------------------------------------------------- #
async def test_reserve_token_mint_requires_ownership_first(redis_socket: str, redis: Redis) -> None:
    guard = build_provider_ownership_guard(_OwnSettings(redis_socket), OwnerRole.BACKEND)
    assert guard is not None
    with pytest.raises(TokenMintThrottledError):
        await guard.reserve_token_mint()  # never acquired ownership → fail closed, no mint
    await guard.release()


async def test_valid_owner_may_reserve_a_mint(redis_socket: str, redis: Redis) -> None:
    guard = build_provider_ownership_guard(_OwnSettings(redis_socket), OwnerRole.BACKEND)
    assert guard is not None
    await guard.acquire_or_fail()
    await guard.reserve_token_mint()  # first mint of a fresh domain: allowed, no raise
    await guard.release()


async def test_successor_reserve_denied_inside_cooldown(redis_socket: str, redis: Redis) -> None:
    a = build_provider_ownership_guard(_OwnSettings(redis_socket), OwnerRole.BACKEND)
    assert a is not None
    await a.acquire_or_fail()
    await a.reserve_token_mint()  # A mints
    await a.release()

    b = build_provider_ownership_guard(_OwnSettings(redis_socket), OwnerRole.INGESTION)
    assert b is not None
    await b.acquire_or_fail()  # B legitimately owns now
    with pytest.raises(TokenMintThrottledError):
        await b.reserve_token_mint()  # but the cross-process cooldown blocks an immediate re-mint
    await b.release()


# --------------------------------------------------------------------------- #
# PART I/Q — backend↔ingestion handoff: peak owner <= 1, fence rises, cooldown holds
# --------------------------------------------------------------------------- #
async def test_handoff_keeps_single_owner_rising_fence_and_respects_cooldown(
    redis_socket: str, redis: Redis
) -> None:
    a = build_provider_ownership_guard(_OwnSettings(redis_socket), OwnerRole.BACKEND)
    b = build_provider_ownership_guard(_OwnSettings(redis_socket), OwnerRole.INGESTION)
    assert a is not None and b is not None
    try:
        await a.acquire_or_fail()
        await a.reserve_token_mint()
        fence_a = a.fencing_generation
        # While A owns, B cannot acquire → at most one authorized owner at any time.
        with pytest.raises(OwnershipAcquisitionError):
            await b.acquire_or_fail()

        await a.release()  # governed handoff: predecessor releases first (0-owner gap allowed)
        await b.acquire_or_fail()
        assert b.fencing_generation > fence_a  # fence strictly increases across the handoff
        with pytest.raises(TokenMintThrottledError):
            await b.reserve_token_mint()  # A minted recently → successor waits out the cooldown

        await redis.delete(_MINT_KEY)  # the cooldown window elapses
        await b.reserve_token_mint()  # now the successor may mint
        fence_b = b.fencing_generation

        # Reverse handoff back to backend.
        await b.release()
        c = build_provider_ownership_guard(_OwnSettings(redis_socket), OwnerRole.BACKEND)
        assert c is not None
        await c.acquire_or_fail()
        assert fence_b is not None and c.fencing_generation > fence_b
        await c.release()
    finally:
        await a.release()
        await b.release()


# --------------------------------------------------------------------------- #
# PART R — crashed owner's lease blocks a successor until TTL; cooldown still holds
# --------------------------------------------------------------------------- #
async def test_crashed_owner_blocks_successor_until_ttl_then_cooldown_holds(
    redis_socket: str, redis: Redis
) -> None:
    a = build_provider_ownership_guard(_OwnSettings(redis_socket), OwnerRole.BACKEND)
    assert a is not None
    await a.acquire_or_fail()
    await a.reserve_token_mint()
    fence_a = a.fencing_generation
    # A "crashes": it never releases; its lease record persists in Redis.

    b = build_provider_ownership_guard(_OwnSettings(redis_socket), OwnerRole.INGESTION)
    assert b is not None
    with pytest.raises(OwnershipAcquisitionError):
        await b.acquire_or_fail()  # cannot acquire before the crashed lease expires

    await redis.delete(_OWNER_KEY)  # simulate the lease TTL expiring
    await b.acquire_or_fail()
    assert b.fencing_generation > fence_a  # successor takes a strictly higher fence
    with pytest.raises(TokenMintThrottledError):
        await b.reserve_token_mint()  # the mint cooldown survives the crash (persisted metadata)
    await b.release()


# --------------------------------------------------------------------------- #
# PART S — default inertness: ownership disabled builds no ownership/token Redis client
# --------------------------------------------------------------------------- #
async def test_default_disabled_builds_no_guard(redis_socket: str) -> None:
    guard = build_provider_ownership_guard(
        _OwnSettings(redis_socket, enabled=False), OwnerRole.BACKEND
    )
    assert guard is None  # no coordinator, no token guard, no Redis client, no activity


# --------------------------------------------------------------------------- #
# PART E — diagnostic tool refuses to open Dhan while a governed owner is live
# --------------------------------------------------------------------------- #
async def test_diagnostic_refused_while_a_governed_owner_holds_the_lease(
    redis_socket: str, redis: Redis
) -> None:
    from app.tools.session_ohlc_evidence.collect import (
        DiagnosticOwnershipConflictError,
        _acquire_diagnostic_ownership,
    )

    owner = build_provider_ownership_guard(_OwnSettings(redis_socket), OwnerRole.BACKEND)
    assert owner is not None
    await owner.acquire_or_fail()
    try:
        with pytest.raises(DiagnosticOwnershipConflictError):
            await _acquire_diagnostic_ownership(_OwnSettings(redis_socket))
    finally:
        await owner.release()

    # With the owner gone, the diagnostic may acquire the shared lease and then releases it.
    guard = await _acquire_diagnostic_ownership(_OwnSettings(redis_socket))
    assert guard is not None
    from app.tools.session_ohlc_evidence.collect import _release_diagnostic_ownership

    await _release_diagnostic_ownership(guard)


async def test_diagnostic_refused_within_cooldown_and_records_its_mint(
    redis_socket: str, redis: Redis
) -> None:
    from app.tools.session_ohlc_evidence.collect import (
        _acquire_diagnostic_ownership,
        _release_diagnostic_ownership,
    )

    # A governed owner mints then releases; a diagnostic within the cooldown must refuse (Gate H) —
    # not silently mint and poison a later governed owner into a double-mint.
    owner = build_provider_ownership_guard(_OwnSettings(redis_socket), OwnerRole.BACKEND)
    assert owner is not None
    await owner.acquire_or_fail()
    await owner.reserve_token_mint()
    await owner.release()

    with pytest.raises(TokenMintThrottledError):
        await _acquire_diagnostic_ownership(_OwnSettings(redis_socket))
    assert await redis.get(_OWNER_KEY) is None  # the refused diagnostic released the lease

    # After the cooldown elapses the diagnostic may run AND records its own mint.
    await redis.delete(_MINT_KEY)
    guard = await _acquire_diagnostic_ownership(_OwnSettings(redis_socket))
    assert guard is not None
    assert (
        await redis.get(_MINT_KEY) is not None
    )  # the diagnostic recorded its mint (shared throttle)
    await _release_diagnostic_ownership(guard)


async def test_diagnostic_unguarded_when_ownership_disabled(redis_socket: str) -> None:
    from app.tools.session_ohlc_evidence.collect import _acquire_diagnostic_ownership

    guard = await _acquire_diagnostic_ownership(_OwnSettings(redis_socket, enabled=False))
    assert guard is None  # ownership off → the tool runs unguarded exactly as before


# --------------------------------------------------------------------------- #
# PART N — ownership loss fail-close publishes a fenced non-healthy md:health
# --------------------------------------------------------------------------- #
class _FakeProvider(BrokerAdapter):
    capabilities = frozenset({ProviderCapability.LIVE_MARKET_DATA})

    def __init__(self, events: list[Tick]) -> None:
        self._events = list(events)
        self._stop = __import__("asyncio").Event()
        self.connect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        return None

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def stream_market_data(self, request: SubscriptionRequest):  # noqa: ARG002 - stub feed
        for event in self._events:
            yield event
        await self._stop.wait()

    def release(self) -> None:
        self._stop.set()


class _FixedTradingDate:
    def current_trading_date(self) -> date:
        return _TD


async def _no_sleep(_seconds: float) -> None:
    return None


def _tick(symbol: str = "TCS") -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        event_timestamp=_NOW,
        last_price=Decimal("100.5"),
    )


def _publisher_flags() -> PhaseHFlags:
    return PhaseHFlags(
        market_ingestion_service_enabled=True,
        ipc_publisher_enabled=True,
        ipc_consumer_enabled=False,
        ipc_shadow_compare_enabled=False,
        ipc_authoritative_enabled=False,
        legacy_market_path_enabled=True,
    )


async def test_ownership_loss_publishes_fenced_down_health(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    import asyncio

    from app.market_ipc.health import IngestionHealthPublisher

    config = MarketIpcConfig(block_ms=0)
    prod: Redis = Redis(unix_socket_path=redis_socket)
    stack = build_publication_stack(
        redis=prod,
        config=config,
        producer_id=_PRODUCER,
        state_dir=tmp_path,
        now=lambda: _NOW,
        trading_date_source=_FixedTradingDate(),
        universe_version=7,
    )
    guard = build_provider_ownership_guard(_OwnSettings(redis_socket), OwnerRole.INGESTION)
    assert guard is not None
    provider = _FakeProvider([_tick()])
    service = MarketIngestionService(
        flags=_publisher_flags(),
        provider=provider,
        subscription_request=SubscriptionRequest(
            instruments=(Instrument(exchange="NSE", symbol="TCS"),),
            data_types=frozenset({MarketDataKind.TICK}),
        ),
        publication=stack,
        ownership=guard,
        health_publisher=IngestionHealthPublisher(prod, config),
        supervisor_sleep=_no_sleep,
        observer_interval_seconds=0.002,
        now=lambda: _NOW,
    )
    reader = IngestionHealthReader(redis, config)
    try:
        await service.start()
        # md:health advertises HEALTHY while the incarnation owns and runs.
        for _ in range(500):
            state = await reader.read()
            if state is not None and state.ingestion is ProviderStatus.HEALTHY:
                break
            await asyncio.sleep(0.005)
        assert (await reader.read()).ingestion is ProviderStatus.HEALTHY

        # Ownership is lost: evict the lease and let the guard detect it (fail closed).
        await redis.delete(_OWNER_KEY)
        assert await guard.validate() is False  # triggers _enter_lost → terminal → _fail_closed
        for _ in range(500):
            if service.status is ServiceStatus.FAILED:
                break
            await asyncio.sleep(0.005)
        assert service.status is ServiceStatus.FAILED

        final = await reader.read()
        assert final is not None
        assert final.ingestion is ProviderStatus.DOWN  # no longer advertising fresh HEALTHY
        assert final.transport is ProviderStatus.DOWN
        assert (
            final.terminal_publication_break is False
        )  # ownership loss is NOT a publication break
        assert final.producer_id == _PRODUCER
    finally:
        provider.release()
        await service.stop()
        await prod.aclose()


async def test_health_key_constant_matches_config() -> None:
    # Guards the fenced-write test against a silent key rename.
    assert health_key(MarketIpcConfig()) == "md:health"
