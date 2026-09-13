"""Real disposable-Redis failure paths for the H3A shadow-publish pipeline (DECOUPLING PHASE H3B).

Complements the H3A happy-path integration suite by driving failure/recovery against a real
``redis-server`` (redislite, private unix socket — never shared/production): a mid-publication
Redis outage fails closed without a false success, a failed start still consumes its durable M1
epoch so the next incarnation advances, a recoverable provider disconnect never turns terminal or
loses accepted events, and at-least-once transport can duplicate a stream entry under one canonical
identity (future-C1 compatible). NO real Dhan, NO consumer, NO C1, NO backend TickEngine.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis

from app.adapters.base.provider_coordinator import ProviderInitializationError
from app.market_ingestion.mode import PhaseHFlags
from app.market_ingestion.publication import build_publication_stack
from app.market_ingestion.service import MarketIngestionService, ServiceStatus
from app.market_ipc.config import MarketIpcConfig
from app.market_ipc.envelope import decode_envelope
from app.market_ipc.publisher import PublishOutcome
from app.schemas.market_data import (
    Instrument,
    MarketData,
    MarketDataKind,
    ProviderHealth,
    ProviderStatus,
    SubscriptionRequest,
    Tick,
)

redislite = pytest.importorskip("redislite", reason="disposable real Redis unavailable")

_NOW = datetime(2026, 9, 13, 4, 15, 0, tzinfo=UTC)
_TD = date(2026, 9, 13)
_PRODUCER = "market-ingestion"


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


def _instrument(symbol: str = "TCS") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _tick(symbol: str = "TCS") -> Tick:
    return Tick(instrument=_instrument(symbol), event_timestamp=_NOW, last_price=Decimal("100.5"))


def _flags() -> PhaseHFlags:
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
        instruments=(_instrument(),), data_types=frozenset({MarketDataKind.TICK})
    )


async def _yield_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


async def _until(predicate: object, *, limit: int = 100_000) -> None:
    for _ in range(limit):
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was never reached")


class _Provider:
    """Yields one episode then ends."""

    def __init__(self, events: list[MarketData]) -> None:
        self._events = events
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.stream_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def stream_market_data(self, _request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        self.stream_calls += 1
        for event in self._events:
            yield event


class _UnhealthyProvider(_Provider):
    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.UNHEALTHY, observed_at=_NOW)


class _GatedProvider:
    """Yields the first events, awaits a gate (so a test can cut Redis), then yields the rest."""

    def __init__(
        self, first: list[MarketData], gate: asyncio.Event, rest: list[MarketData]
    ) -> None:
        self._first = first
        self._gate = gate
        self._rest = rest
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.stream_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def stream_market_data(self, _request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        self.stream_calls += 1
        for event in self._first:
            yield event
        await self._gate.wait()
        for event in self._rest:
            yield event


class _DropOnceProvider:
    """Episode 1 yields a tick then drops (recoverable); episode 2 yields a tick then ends."""

    def __init__(self) -> None:
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.stream_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def stream_market_data(self, _request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        self.stream_calls += 1
        yield _tick("A")
        if self.stream_calls == 1:
            raise ConnectionError("simulated transport drop")  # recoverable → reconnect


def _service(
    redis: Redis,
    provider: object,
    state_dir: str,
    *,
    max_reconnects: int | None = 0,
) -> MarketIngestionService:
    stack = build_publication_stack(
        redis=redis,
        config=MarketIpcConfig(),
        producer_id=_PRODUCER,
        state_dir=state_dir,  # type: ignore[arg-type]
        now=lambda: _NOW,
        trading_date=_TD,
    )
    return MarketIngestionService(
        flags=_flags(),
        provider=provider,  # type: ignore[arg-type]
        subscription_request=_request(),
        publication=stack,
        supervisor_max_reconnects=max_reconnects,
        supervisor_sleep=_yield_sleep,
        observer_interval_seconds=0.0,
        observer_sleep=_yield_sleep,
    )


async def test_redis_outage_during_publication_fails_closed(tmp_path) -> None:
    server = redislite.Redis()
    client: Redis = Redis(unix_socket_path=server.socket_file)
    await client.flushall()
    gate = asyncio.Event()
    provider = _GatedProvider([_tick("A")], gate, [_tick("B")])
    service = _service(client, provider, str(tmp_path))
    try:
        await service.start()
        await _until(lambda: service.diagnostics().published_total >= 1)  # A landed on Redis
        server.shutdown()  # the outage begins
        gate.set()  # provider now yields B → its transmit hits a dead Redis
        await asyncio.wait_for(service._watch_task, timeout=5.0)  # fail closed

        diagnostics = service.diagnostics()
        assert service.status is ServiceStatus.FAILED
        assert service.terminal_failure is True
        assert diagnostics.published_total == 1  # B is never confirmed — no false success
        assert diagnostics.continuity_state == "broken"
        assert provider.disconnect_calls >= 1
        await service.stop()  # idempotent; touches no Redis
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


async def test_failed_start_consumes_epoch_and_next_incarnation_advances(
    redis: Redis, tmp_path
) -> None:
    # Frozen order allocates the durable M1 epoch (boundary.start) BEFORE the provider health
    # check, so a start that fails at an unhealthy provider still consumes the epoch.
    failed = _service(redis, _UnhealthyProvider([_tick()]), str(tmp_path))
    with pytest.raises(ProviderInitializationError):
        await failed.start()
    assert failed.status is ServiceStatus.FAILED
    assert failed.diagnostics().producer_epoch == 1  # epoch was consumed on the failed start

    healthy = _service(redis, _Provider([_tick()]), str(tmp_path))  # same durable state dir
    await healthy.start()
    await healthy.wait()
    await healthy.stop()
    assert healthy.diagnostics().producer_epoch == 2  # never reuses the failed incarnation's epoch


async def test_recoverable_disconnect_is_not_terminal_and_keeps_events(
    redis: Redis, tmp_path
) -> None:
    provider = _DropOnceProvider()
    service = _service(redis, provider, str(tmp_path), max_reconnects=1)
    await service.start()
    await service.wait()  # episode 1 drops, reconnects, episode 2 ends
    await service.stop()

    assert service.terminal_failure is False  # a transport drop is recoverable, never terminal
    assert provider.connect_calls == 1  # coordinator/auth not re-run on a transport reconnect
    assert service.diagnostics().producer_epoch == 1  # epoch stable across the reconnect
    assert service.diagnostics().continuity_state == "stopped"  # clean, not broken
    assert await redis.xlen(MarketIpcConfig().stream_name) == 2  # both episodes' ticks survived


async def test_duplicate_transmit_shares_canonical_identity(redis: Redis, tmp_path) -> None:
    # At-least-once transport can append the same envelope twice; both entries carry the SAME
    # canonical identity, so a future C1 consumer can dedup them. No C1 is exercised here.
    stack = build_publication_stack(
        redis=redis,
        config=MarketIpcConfig(),
        producer_id=_PRODUCER,
        state_dir=str(tmp_path),  # type: ignore[arg-type]
        now=lambda: _NOW,
        trading_date=_TD,
    )
    await stack.boundary.start()  # allocates the epoch, ensures the group
    envelope = stack.publisher.prepare(_tick("A"))
    assert not isinstance(envelope, PublishOutcome)  # prepared successfully, not a rejection
    await stack.publisher.transmit(envelope)  # type: ignore[arg-type]
    await stack.publisher.transmit(envelope)  # type: ignore[arg-type]  # at-least-once redelivery
    await stack.boundary.stop()

    config = MarketIpcConfig()
    assert await redis.xlen(config.stream_name) == 2  # two physical stream entries
    entries = await redis.xrange(config.stream_name)
    identities = {
        (
            decode_envelope(fields[b"e"]).producer_id,
            decode_envelope(fields[b"e"]).producer_epoch,
            decode_envelope(fields[b"e"]).producer_sequence,
        )
        for _id, fields in entries
    }
    assert identities == {(_PRODUCER, 1, 1)}  # one canonical identity across both entries
