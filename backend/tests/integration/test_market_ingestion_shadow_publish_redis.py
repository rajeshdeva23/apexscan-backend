"""Real disposable-Redis integration for the H3A shadow-publish pipeline (DECOUPLING PHASE H3A).

Drives the decoupled pipeline end to end against a real ``redis-server`` (redislite, private unix
socket — never shared/production): fake provider → :class:`MarketIngestionService` (publisher mode)
→ :class:`PublishingEventSink` → M2 → D1 → Redis, with L1 tracking. Proves canonical events land in
the stream in FIFO order with monotonic identity, that a MarketReference commits the compacted
reference hash atomically, that the M1 epoch is allocated once, and that a clean drain publishes
everything. NO real Dhan, NO consumer, NO C1, NO backend TickEngine. Skips if redislite is absent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis

from app.market_ingestion.mode import PhaseHFlags
from app.market_ingestion.publication import build_publication_stack
from app.market_ingestion.service import MarketIngestionService, ServiceStatus
from app.market_ipc.config import MarketIpcConfig
from app.market_ipc.envelope import decode_envelope
from app.market_ipc.state import reference_key
from app.schemas.market_data import (
    Instrument,
    MarketData,
    MarketDataKind,
    MarketReference,
    ProviderHealth,
    ProviderStatus,
    Quote,
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


def _quote() -> Quote:
    return Quote(
        instrument=_instrument(),
        event_timestamp=_NOW,
        bid_price=Decimal("101.10"),
        ask_price=Decimal("101.40"),
        bid_quantity=10,
        ask_quantity=20,
    )


def _reference() -> MarketReference:
    return MarketReference(instrument=_instrument(), previous_close=Decimal("99.25"))


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
    await asyncio.sleep(0)  # yield to the loop (never starve the observer poll)


class _FakeProvider:
    """BrokerAdapter + LiveMarketDataAdapter fake yielding one episode of canonical events."""

    def __init__(self, events: list[MarketData]) -> None:
        self._events = events
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        for event in self._events:
            yield event  # ends normally after the episode


def _service(redis: Redis, provider: _FakeProvider, state_dir: str) -> MarketIngestionService:
    config = MarketIpcConfig()
    stack = build_publication_stack(
        redis=redis,
        config=config,
        producer_id=_PRODUCER,
        state_dir=state_dir,  # type: ignore[arg-type]  # Path-like str accepted by DurableEpochAllocator
        now=lambda: _NOW,
        trading_date=_TD,
    )
    return MarketIngestionService(
        flags=_flags(),
        provider=provider,  # type: ignore[arg-type]
        subscription_request=_request(),
        publication=stack,
        supervisor_max_reconnects=0,  # stop after the single episode ends
        supervisor_sleep=_yield_sleep,
        observer_interval_seconds=0.0,
        observer_sleep=_yield_sleep,
    )


async def _run_and_stop(service: MarketIngestionService) -> None:
    await service.start()
    assert service.status is ServiceStatus.RUNNING
    await service.wait()  # supervisor ends after the episode
    await service.stop()  # bounded drain publishes everything queued


async def test_shadow_publish_lands_canonical_events_in_order(redis: Redis, tmp_path) -> None:
    events: list[MarketData] = [_tick("A"), _quote(), _tick("B"), _reference()]
    provider = _FakeProvider(events)
    await _run_and_stop(_service(redis, provider, str(tmp_path)))

    config = MarketIpcConfig()
    assert await redis.xlen(config.stream_name) == 4  # every canonical event published
    entries = await redis.xrange(config.stream_name)
    sequences = [decode_envelope(fields[b"e"]).producer_sequence for _id, fields in entries]
    assert sequences == [1, 2, 3, 4]  # single-worker FIFO order + monotonic identity


async def test_market_reference_commits_compacted_reference_atomically(
    redis: Redis, tmp_path
) -> None:
    provider = _FakeProvider([_reference()])
    await _run_and_stop(_service(redis, provider, str(tmp_path)))

    config = MarketIpcConfig()
    assert await redis.xlen(config.stream_name) == 1  # stream record written
    stored = await redis.hget(reference_key(config.reference_key_prefix, _TD), "NSE:TCS")
    assert stored is not None  # reference hash committed atomically with the stream (D1)


async def test_epoch_allocated_once_and_accepted_position_advances(
    redis: Redis, tmp_path
) -> None:
    provider = _FakeProvider([_tick("A"), _tick("B"), _tick("C")])
    service = _service(redis, provider, str(tmp_path))
    await _run_and_stop(service)

    diagnostics = service.diagnostics()
    assert diagnostics.producer_epoch == 1  # first incarnation on a fresh state dir
    assert diagnostics.last_accepted_sequence == 3
    assert diagnostics.published_total == 3


async def test_restart_allocates_a_new_epoch(redis: Redis, tmp_path) -> None:
    first = _service(redis, _FakeProvider([_tick()]), str(tmp_path))
    await _run_and_stop(first)
    assert first.diagnostics().producer_epoch == 1

    # A whole new service incarnation over the SAME durable state dir → new, higher epoch.
    second = _service(redis, _FakeProvider([_tick()]), str(tmp_path))
    await _run_and_stop(second)
    assert second.diagnostics().producer_epoch == 2  # M1 never reuses an epoch


class _ReconnectingProvider:
    """Provider whose first stream episode raises (transport drop) then a second episode ends."""

    def __init__(self) -> None:
        self._episode = 0
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        self._episode += 1
        yield _tick("A")
        if self._episode == 1:
            raise ConnectionError("simulated transport drop")  # recoverable → reconnect


async def test_reconnect_preserves_producer_epoch(redis: Redis, tmp_path) -> None:
    provider = _ReconnectingProvider()
    stack = build_publication_stack(
        redis=redis,
        config=MarketIpcConfig(),
        producer_id=_PRODUCER,
        state_dir=str(tmp_path),  # type: ignore[arg-type]
        now=lambda: _NOW,
        trading_date=_TD,
    )
    service = MarketIngestionService(
        flags=_flags(),
        provider=provider,  # type: ignore[arg-type]
        subscription_request=_request(),
        publication=stack,
        supervisor_max_reconnects=1,  # allow the one reconnect, then stop deterministically
        supervisor_sleep=_yield_sleep,
        observer_interval_seconds=0.0,
        observer_sleep=_yield_sleep,
    )
    await service.start()
    await service.wait()
    await service.stop()

    assert provider.connect_calls == 1  # transport reconnect never re-connects the coordinator/auth
    assert service.diagnostics().producer_epoch == 1  # same epoch across the reconnect
    assert await redis.xlen(MarketIpcConfig().stream_name) == 2  # both episodes' ticks published


async def test_clean_drain_reports_stopped(redis: Redis, tmp_path) -> None:
    service = _service(redis, _FakeProvider([_tick(), _tick()]), str(tmp_path))
    await _run_and_stop(service)
    diagnostics = service.diagnostics()
    assert service.status is ServiceStatus.STOPPED
    assert diagnostics.continuity_state == "stopped"
    assert diagnostics.published_total == 2
