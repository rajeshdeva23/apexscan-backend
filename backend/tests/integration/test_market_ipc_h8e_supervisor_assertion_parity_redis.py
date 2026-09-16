"""Decoupled provider-supervisor failure/recovery assertion parity (DECOUPLING PHASE H8E).

The post-H9A provider failure/recovery audit found that most failure/recovery behaviour is already
proven, but a few assertions were weaker on the **go-forward decoupled path** (the real
``MarketIngestionService`` + ``ProviderSupervisor``) than on the legacy in-process runtime. H8E
closes only that asymmetry, offline, over the REAL decoupled composition:

    deterministic fake provider (no Dhan / tokens / sockets / internet)
        -> MarketIngestionService + ProviderSupervisor   (real, go-forward path)
        -> build_publication_stack: M1 -> M2 -> D1 -> L1   (real composition root)
        -> Redis md:events (redislite)
        -> MarketEventConsumer + durable C1 -> RecordingShadowSink -> H4C comparator

Proven here (all offline, deterministic — no correctness gated on a real sleep):
  A  >= 3 consecutive recoverable stream failures self-heal with BOUNDED exponential backoff
     (no tight reconnect loop), the producer epoch is unchanged by a provider reconnect, and the
     stream continues to clean parity;
  B  a provider reconnect never runs two concurrent stream loops (``max_active_streams == 1``);
  C  a repeated/concurrent ``start()`` cannot create a second supervisor task (idempotent boot);
  E  an event burst immediately after a reconnect preserves the existing publication/C1 parity
     contract (no unexplained loss, no unexplained duplicate application).

Test D (``_live_receive_lock`` contention) is intentionally OMITTED: that lock serialises the Dhan
adapter's receive path across MULTIPLE consumers of one adapter (the legacy in-process fan-out); the
decoupled ProviderSupervisor path drives a single consumer, so on the go-forward path Test B already
proves the single-stream-loop invariant. The lock remains structurally in place
(``adapters/dhan/adapter.py`` ``async with self._live_receive_lock``) and covered by the existing
adapter suites; a dedicated two-consumer contention test would exercise a legacy-path concern
orthogonal to H8E's decoupled focus (see the H8E validation report).

OFFLINE and NON-AUTHORITATIVE: no live Dhan, no production contact, no IPC authority, no
TickEngine/MarketContext, no FIX-2 workaround, no cross-process ownership wiring. Timestamps are
canonical tz-aware UTC. Skips cleanly if redislite is absent.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from redis.asyncio import Redis

from app.adapters.base.broker_adapter import BrokerAdapter
from app.market_ingestion.mode import PhaseHFlags
from app.market_ingestion.publication import PublicationStack, build_publication_stack
from app.market_ingestion.service import MarketIngestionService, ServiceStatus
from app.market_ipc import (
    BoundedDeduplicator,
    CompositeDeduplicator,
    DurableDeduplicator,
    MarketEventConsumer,
    MarketEventEnvelope,
    MarketIpcConfig,
    RecordingShadowSink,
    RedisMarketEventStream,
    build_envelope,
    compare,
    view_from_envelope,
    views_from_applied,
)
from app.market_ipc.events import IpcPayload
from app.schemas.market_data import (
    Instrument,
    MarketDataKind,
    MarketReference,
    ProviderCapability,
    ProviderHealth,
    ProviderSessionOhlc,
    ProviderStatus,
    Quote,
    SubscriptionRequest,
    Tick,
)

redislite = pytest.importorskip("redislite", reason="disposable real Redis unavailable")

_NOW = datetime(2026, 9, 9, 10, 15, 30, tzinfo=UTC)
_TD = date(2026, 9, 9)
_PRODUCER = "market-ingestion"
_SYMBOLS = ("TCS", "INFY", "RELIANCE", "HDFC", "WIPRO")
_UNIVERSE = 7
_CUT = object()  # sentinel: dequeuing it ends a stream attempt (a recoverable transport drop)


@pytest.fixture(scope="module")
def redis_socket() -> str:
    server = redislite.Redis()
    try:
        yield server.socket_file
    finally:
        server.shutdown()


@pytest.fixture
async def redis(redis_socket: str) -> Redis:
    """A flush-on-setup probe client (the producer/consumer own their own clients)."""
    client: Redis = Redis(unix_socket_path=redis_socket)
    await client.flushall()
    try:
        yield client
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# Canonical fixture builders (deterministic, tz-aware UTC; no FIX-2 workaround)
# --------------------------------------------------------------------------- #
def _tick(symbol: str = "TCS", price: str = "100.5") -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        event_timestamp=_NOW,
        last_price=Decimal(price),
    )


def _quote(symbol: str = "TCS") -> Quote:
    return Quote(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        event_timestamp=_NOW,
        bid_price=Decimal("100"),
        ask_price=Decimal("101"),
        bid_quantity=5,
        ask_quantity=7,
    )


def _reference(symbol: str = "TCS") -> MarketReference:
    return MarketReference(
        instrument=Instrument(exchange="NSE", symbol=symbol), previous_close=Decimal("99.25")
    )


def _tick_with_ohlc(symbol: str = "TCS") -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        event_timestamp=_NOW,
        last_price=Decimal("100.5"),
        traded_quantity=10,
        session_cumulative_volume=1_000,
        session_ohlc=ProviderSessionOhlc(
            open_price=Decimal("99"),
            high_price=Decimal("101"),
            low_price=Decimal("98"),
            close_price=Decimal("100.5"),
        ),
    )


def _mix(count: int, *, offset: int = 0) -> list[IpcPayload]:
    """Deterministic canonical events across instruments and all IPC-supported kinds."""
    events: list[IpcPayload] = []
    for i in range(offset, offset + count):
        symbol = _SYMBOLS[i % len(_SYMBOLS)]
        selector = i % 4
        if selector == 0:
            events.append(_tick(symbol=symbol, price=str(100 + (i % 50))))
        elif selector == 1:
            events.append(_quote(symbol=symbol))
        elif selector == 2:
            events.append(_reference(symbol=symbol))
        else:
            events.append(_tick_with_ohlc(symbol=symbol))
    return events


def _envelope(payload: IpcPayload, *, seq: int, epoch: int) -> MarketEventEnvelope:
    return build_envelope(
        payload,
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=_UNIVERSE,
    )


def _expected_views(events: list[IpcPayload], *, epoch: int, start_seq: int = 1) -> list[object]:
    """Views for one incarnation's contiguous batch (provider reconnect never resets sequence)."""
    return [
        view_from_envelope(_envelope(datum, seq=start_seq + i, epoch=epoch))
        for i, datum in enumerate(events)
    ]


class _FixedTradingDate:
    """Deterministic producer trading-date source (canonical UTC session date; no host clock)."""

    def current_trading_date(self) -> date:
        return _TD


def _request() -> SubscriptionRequest:
    return SubscriptionRequest(
        instruments=tuple(Instrument(exchange="NSE", symbol=s) for s in _SYMBOLS),
        data_types=frozenset({MarketDataKind.TICK}),
    )


def _flags() -> PhaseHFlags:
    return PhaseHFlags(
        market_ingestion_service_enabled=True,
        ipc_publisher_enabled=True,
        ipc_consumer_enabled=False,
        ipc_shadow_compare_enabled=False,
        ipc_authoritative_enabled=False,
        legacy_market_path_enabled=True,
    )


def _config(**overrides: object) -> MarketIpcConfig:
    return MarketIpcConfig(block_ms=0, **overrides)


class _CapturingSleeper:
    """A supervisor backoff sleeper that RECORDS each requested delay but never really waits.

    Correctness is asserted on the recorded delay VALUES (bounded exponential growth), not on
    elapsed wall-time; the coroutine only yields to the event loop.
    """

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# Concurrency-instrumented reconnecting provider (the new H8E fixture)
# --------------------------------------------------------------------------- #
class _CountingReconnectingProvider(BrokerAdapter):
    """Fake provider whose stream drops recoverably on ``cut()``, counting concurrent stream loops.

    ``current_active_streams``/``max_active_streams`` prove the supervisor never runs two provider
    receive loops at once (a reconnect must fully exit the old stream before the next owns the
    provider). ``connect`` is called once by the coordinator; a reconnect only re-enters
    ``stream_market_data`` (``stream_calls`` grows, ``connect_calls`` stays 1). No Dhan/socket/net.
    """

    capabilities = frozenset({ProviderCapability.LIVE_MARKET_DATA})

    def __init__(self) -> None:
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.stream_calls = 0
        self.current_active_streams = 0
        self.max_active_streams = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def stream_market_data(self, request: SubscriptionRequest):  # noqa: ARG002 - stub feed
        self.stream_calls += 1
        self.current_active_streams += 1
        self.max_active_streams = max(self.max_active_streams, self.current_active_streams)
        try:
            while True:
                item = await self._queue.get()
                if item is _CUT:
                    raise ConnectionError("simulated recoverable provider transport drop")
                yield item  # type: ignore[misc]
        finally:
            self.current_active_streams -= 1

    def push(self, events: list[IpcPayload]) -> None:
        for datum in events:
            self._queue.put_nowait(datum)

    def cut(self) -> None:
        """End the current stream attempt recoverably (the supervisor will reconnect)."""
        self._queue.put_nowait(_CUT)


# --------------------------------------------------------------------------- #
# Real decoupled ingestion incarnation (publisher mode) over the real composition root
# --------------------------------------------------------------------------- #
class _Ingestion:
    def __init__(
        self,
        service: MarketIngestionService,
        stack: PublicationStack,
        provider: _CountingReconnectingProvider,
    ) -> None:
        self.service = service
        self.stack = stack
        self.provider = provider

    @property
    def epoch(self) -> int:
        epoch = self.service.diagnostics().producer_epoch
        assert epoch is not None
        return epoch

    @property
    def published_total(self) -> int:
        return self.service.diagnostics().published_total

    @property
    def reconnect_total(self) -> int:
        return self.service.diagnostics().reconnect_total

    async def publish(self, events: list[IpcPayload]) -> None:
        target = self.published_total + len(events)
        self.provider.push(events)
        await self._await(lambda: self.published_total >= target, f"published < {target}")

    async def await_reconnect(self, target: int) -> None:
        await self._await(lambda: self.reconnect_total >= target, f"reconnect < {target}")

    @staticmethod
    async def _await(predicate, message: str) -> None:
        for _ in range(300_000):  # condition-gated; the cap is deadlock protection, not a timer
            if predicate():
                return
            await asyncio.sleep(0.001)
        raise AssertionError(message)

    async def stop(self) -> None:
        await self.service.stop()


def _build_service(
    provider: _CountingReconnectingProvider,
    stack: PublicationStack,
    *,
    sleep,
    max_reconnects: int | None,
) -> MarketIngestionService:
    return MarketIngestionService(
        flags=_flags(),
        provider=provider,
        subscription_request=_request(),
        publication=stack,
        supervisor_max_reconnects=max_reconnects,
        supervisor_sleep=sleep,
        observer_interval_seconds=0.005,
    )


async def _start_ingestion(
    redis_socket: str,
    config: MarketIpcConfig,
    state_dir: Path,
    *,
    sleep,
    max_reconnects: int | None = None,
) -> _Ingestion:
    provider = _CountingReconnectingProvider()
    stack = build_publication_stack(
        redis=Redis(unix_socket_path=redis_socket),
        config=config,
        producer_id=_PRODUCER,
        state_dir=state_dir,
        now=lambda: _NOW,
        trading_date_source=_FixedTradingDate(),
        universe_version=_UNIVERSE,
    )
    service = _build_service(provider, stack, sleep=sleep, max_reconnects=max_reconnects)
    await service.start()
    return _Ingestion(service, stack, provider)


# --------------------------------------------------------------------------- #
# Backend consumer incarnation (own client, empty memory cache, durable C1)
# --------------------------------------------------------------------------- #
async def _start_backend(redis_socket: str, config: MarketIpcConfig, name: str):
    client: Redis = Redis(unix_socket_path=redis_socket)
    cfg = config.model_copy(update={"consumer_name": name})
    sink = RecordingShadowSink(max_entries=40_000)
    consumer = MarketEventConsumer(
        transport=RedisMarketEventStream(redis=client, config=cfg),
        config=cfg,
        sink=sink,
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: _UNIVERSE,
        now=lambda: _NOW,
        deduplicator=CompositeDeduplicator(
            memory=BoundedDeduplicator(cfg.dedup_max_entries),
            durable=DurableDeduplicator(client, cfg),
        ),
    )
    await consumer.start()
    return client, consumer, sink, cfg


async def _drain(consumer: MarketEventConsumer, client: Redis, cfg: MarketIpcConfig) -> None:
    for _ in range(4_000):
        before = consumer.diagnostics().acked_total
        await consumer.poll_once()
        pending = int((await client.xpending(cfg.stream_name, cfg.consumer_group))["pending"])
        if consumer.diagnostics().acked_total == before and pending == 0:
            return
    raise AssertionError("stream did not drain within the cycle budget")


# =========================================================================== #
# H8E-A: multi-cycle recoverable failure -> bounded backoff + clean continuation
# =========================================================================== #
async def test_h8e_a_multicycle_recoverable_backoff_and_epoch_stable(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config(read_count=200)
    sleeper = _CapturingSleeper()
    backend_client, consumer, sink, cfg = await _start_backend(redis_socket, config, "backend-a")
    ingestion = await _start_ingestion(
        redis_socket, config, tmp_path, sleep=sleeper, max_reconnects=None
    )
    epoch0 = ingestion.epoch

    all_events: list[IpcPayload] = []
    # Three consecutive recoverable drops, each followed by a self-healed reconnect + continuation.
    for cycle in range(3):
        batch = _mix(10, offset=cycle * 10)
        await ingestion.publish(batch)
        all_events.extend(batch)
        ingestion.provider.cut()
        await ingestion.await_reconnect(cycle + 1)
    final = _mix(10, offset=30)  # successful continuation after the 3rd reconnect
    await ingestion.publish(final)
    all_events.extend(final)

    # Bounded exponential backoff, no tight loop, and NOT a terminal transition.
    assert sleeper.calls == [1.0, 2.0, 4.0]  # 1*2^0, 1*2^1, 1*2^2 (ProviderSupervisor._backoff)
    assert all(0 < delay <= 30.0 for delay in sleeper.calls)  # bounded, never a zero-delay spin
    assert ingestion.reconnect_total == 3
    assert ingestion.service.status is ServiceStatus.RUNNING
    assert ingestion.service.terminal_failure is False
    # A provider reconnect must NOT mint a new producer epoch (§13).
    assert ingestion.epoch == epoch0

    await _drain(consumer, backend_client, cfg)
    report = compare(_expected_views(all_events, epoch=epoch0), views_from_applied(sink.events))
    assert report.is_clean
    assert report.matched_total == 40
    assert report.missing_total == 0 and report.unexpected_total == 0

    await ingestion.stop()
    assert ingestion.service.status is ServiceStatus.STOPPED
    assert ingestion.provider.current_active_streams == 0  # clean shutdown, no lingering loop
    await backend_client.aclose()


# =========================================================================== #
# H8E-B: a provider reconnect never runs two concurrent stream loops
# =========================================================================== #
async def test_h8e_b_exactly_one_active_stream_loop(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config(read_count=200)
    ingestion = await _start_ingestion(
        redis_socket, config, tmp_path, sleep=_CapturingSleeper(), max_reconnects=None
    )
    for cycle in range(4):
        await ingestion.publish(_mix(5, offset=cycle * 5))
        ingestion.provider.cut()
        await ingestion.await_reconnect(cycle + 1)
    await ingestion.publish(_mix(5, offset=20))

    # The core invariant: across every reconnect only one stream loop was ever active at once.
    assert ingestion.provider.max_active_streams == 1
    assert ingestion.provider.current_active_streams == 1  # exactly one live loop right now
    assert ingestion.provider.connect_calls == 1  # a reconnect re-iterates the stream, no re-auth
    assert ingestion.provider.stream_calls == 5  # initial + 4 reconnects

    await ingestion.stop()
    assert ingestion.provider.current_active_streams == 0


# =========================================================================== #
# H8E-C: a repeated / concurrent start() cannot create a second supervisor loop
# =========================================================================== #
async def test_h8e_c_repeated_start_is_idempotent_no_second_loop(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config(read_count=200)
    ingestion = await _start_ingestion(
        redis_socket, config, tmp_path, sleep=_CapturingSleeper(), max_reconnects=None
    )
    epoch0 = ingestion.epoch

    # A second sequential start() must be a no-op — never a second connect / stream loop.
    await ingestion.service.start()
    for _ in range(5):  # give any (erroneously) spawned second stream loop a chance to enter
        await asyncio.sleep(0)
    assert ingestion.provider.connect_calls == 1
    assert ingestion.provider.max_active_streams == 1
    assert ingestion.epoch == epoch0
    assert ingestion.service.status is ServiceStatus.RUNNING

    # Events still flow through the single surviving loop.
    await ingestion.publish(_mix(6))
    assert ingestion.provider.max_active_streams == 1

    await ingestion.stop()
    assert ingestion.provider.current_active_streams == 0


async def test_h8e_c_concurrent_start_is_idempotent_no_second_loop(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config(read_count=200)
    provider = _CountingReconnectingProvider()
    stack = build_publication_stack(
        redis=Redis(unix_socket_path=redis_socket),
        config=config,
        producer_id=_PRODUCER,
        state_dir=tmp_path,
        now=lambda: _NOW,
        trading_date_source=_FixedTradingDate(),
        universe_version=_UNIVERSE,
    )
    service = _build_service(provider, stack, sleep=_CapturingSleeper(), max_reconnects=None)

    # Two concurrent start() coroutines must not both boot the provider.
    await asyncio.gather(service.start(), service.start())
    for _ in range(5):
        await asyncio.sleep(0)
    assert provider.connect_calls == 1
    assert provider.max_active_streams == 1
    assert service.status is ServiceStatus.RUNNING

    await service.stop()
    assert provider.current_active_streams == 0


# =========================================================================== #
# H8E-E: an event burst immediately after a reconnect preserves the parity contract
# =========================================================================== #
async def test_h8e_e_burst_immediately_after_reconnect(
    redis_socket: str, redis: Redis, tmp_path: Path
) -> None:
    config = _config(read_count=500)
    backend_client, consumer, sink, cfg = await _start_backend(redis_socket, config, "backend-e")
    ingestion = await _start_ingestion(
        redis_socket, config, tmp_path, sleep=_CapturingSleeper(), max_reconnects=None
    )
    epoch0 = ingestion.epoch

    initial = _mix(30, offset=0)
    await ingestion.publish(initial)
    ingestion.provider.cut()
    await ingestion.await_reconnect(1)

    burst = _mix(500, offset=30)  # immediate high-volume burst on the freshly reconnected stream
    await ingestion.publish(burst)

    assert ingestion.epoch == epoch0  # burst-after-reconnect stays within one epoch
    assert ingestion.reconnect_total == 1
    assert ingestion.provider.max_active_streams == 1

    await _drain(consumer, backend_client, cfg)
    report = compare(
        _expected_views(initial + burst, epoch=epoch0), views_from_applied(sink.events)
    )
    assert report.is_clean  # no unexplained loss, no unexplained duplicate application
    assert report.matched_total == 530
    assert report.missing_total == 0
    assert report.unexpected_total == 0
    assert report.value_mismatch_total == 0
    assert sink.applied_total == 530  # each canonical identity applied exactly once

    await ingestion.stop()
    await backend_client.aclose()
