"""Long-lived process + Redis-lifecycle readiness against real Redis (DECOUPLING PHASE H3C).

Drives the decoupled shadow-publish pipeline against a real ``redis-server`` (redislite, private
unix socket — never shared/production) to prove the H3C packaging goals: the service-owned Redis
client is closed exactly once, only after M2 has drained; a long-lived incarnation crosses a
trading-date boundary (D1 → D2) without a restart or a new epoch; a reconnect soak accumulates no
tasks/clients and keeps the epoch stable; and a bounded queue survives a thousands-event episode.
NO real Dhan, NO consumer, NO C1, NO backend TickEngine. Skips if redislite is absent.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from redis.asyncio import Redis

from app.adapters.base.provider_coordinator import ProviderInitializationError
from app.market_engine.session import MarketSessionClassifier, SessionSchedule, TradingCalendar
from app.market_ingestion.mode import PhaseHFlags
from app.market_ingestion.publication import (
    PublicationStack,
    SessionTradingDate,
    build_publication_stack,
)
from app.market_ingestion.service import MarketIngestionService, ServiceStatus
from app.market_ipc.config import MarketIpcConfig
from app.market_ipc.state import reference_key
from app.schemas.market_data import (
    Instrument,
    MarketData,
    MarketDataKind,
    MarketReference,
    ProviderHealth,
    ProviderStatus,
    SubscriptionRequest,
    Tick,
)

redislite = pytest.importorskip("redislite", reason="disposable real Redis unavailable")

_NOW = datetime(2026, 9, 14, 6, 0, 0, tzinfo=UTC)  # 2026-09-14 11:30 IST (mid-session, trading day)
_PRODUCER = "market-ingestion"


@pytest.fixture(scope="module")
def redis_socket() -> str:
    server = redislite.Redis()
    try:
        yield server.socket_file
    finally:
        server.shutdown()


def _instrument(symbol: str = "TCS") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _tick(symbol: str = "TCS") -> Tick:
    return Tick(instrument=_instrument(symbol), event_timestamp=_NOW, last_price=Decimal("100.5"))


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
    await asyncio.sleep(0)


async def _until(predicate: Callable[[], bool], *, limit: int = 200_000) -> None:
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was never reached")


def _ref_key(config: MarketIpcConfig, day: date) -> str:
    return reference_key(config.reference_key_prefix, day)


def _classifier() -> MarketSessionClassifier:
    return MarketSessionClassifier(
        schedule=SessionSchedule(
            pre_open_start=time(9, 0),
            opening_auction_start=time(9, 8),
            regular_open=time(9, 15),
            regular_close=time(15, 30),
            closing_end=time(15, 40),
        ),
        calendar=TradingCalendar(),
        exchange_timezone="Asia/Kolkata",
    )


class _Clock:
    """A mutable UTC clock for driving trading-date rollover deterministically."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment


class _SpyRedis:
    """Delegates to a real client; counts ``aclose`` and snapshots stream length at first close.

    Only ``aclose`` is intercepted (to assert close-once + drain-before-close); every other call
    (register_script, xadd, hget, ...) is delegated to the wrapped client unchanged.
    """

    def __init__(self, inner: Redis, stream_name: str) -> None:
        self._inner = inner
        self._stream_name = stream_name
        self.aclose_calls = 0
        self.xlen_at_first_close: int | None = None

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def aclose(self) -> None:
        self.aclose_calls += 1
        if self.xlen_at_first_close is None:
            with contextlib.suppress(Exception):  # a dead server (outage test) has no length
                self.xlen_at_first_close = await self._inner.xlen(self._stream_name)
        await self._inner.aclose()


def _build(
    redis: object,
    provider: object,
    state_dir: str,
    *,
    now: Callable[[], datetime] | None = None,
    max_reconnects: int | None = 0,
    config: MarketIpcConfig | None = None,
) -> tuple[PublicationStack, MarketIngestionService]:
    clock_now = now if now is not None else (lambda: _NOW)
    config = config or MarketIpcConfig()
    stack = build_publication_stack(
        redis=redis,  # type: ignore[arg-type]
        config=config,
        producer_id=_PRODUCER,
        state_dir=state_dir,  # type: ignore[arg-type]
        now=clock_now,
        trading_date_source=SessionTradingDate(classify=_classifier().classify, now=clock_now),
    )
    service = MarketIngestionService(
        flags=_flags(),
        provider=provider,  # type: ignore[arg-type]
        subscription_request=_request(),
        publication=stack,
        supervisor_max_reconnects=max_reconnects,
        supervisor_sleep=_yield_sleep,
        observer_interval_seconds=0.0,
        observer_sleep=_yield_sleep,
    )
    return stack, service


class _Provider:
    """Yields one episode of events then ends."""

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


class _GatedRefProvider:
    """Yields a reference, awaits a gate (the test advances the clock), then yields another."""

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate
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
        yield _reference()
        await self._gate.wait()
        yield _reference()


class _DropThenGatedRefProvider:
    """Episode 1 yields a ref then drops (recoverable); episode 2 gates then yields a ref."""

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate
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
        if self.stream_calls == 1:
            yield _reference()
            raise ConnectionError("simulated transport drop")  # recoverable → reconnect
        await self._gate.wait()
        yield _reference()


class _DropNProvider:
    """First ``drops`` episodes yield a tick then drop; a final episode yields a tick and ends."""

    def __init__(self, drops: int) -> None:
        self._drops = drops
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
        if self.stream_calls <= self._drops:
            raise ConnectionError("simulated transport drop")


class _ManyTicksProvider:
    """Yields ``count`` ticks, periodically yielding to the loop so the worker drains the queue."""

    def __init__(self, count: int) -> None:
        self._count = count
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
        for index in range(self._count):
            yield _tick("A")
            if index % 16 == 0:
                await asyncio.sleep(0)  # let the single worker drain → bounded queue depth


# --------------------------------------------------------------------------- #
# Redis client lifecycle (§4-§7)
# --------------------------------------------------------------------------- #
async def test_clean_shutdown_closes_redis_once_after_drain(redis_socket, tmp_path) -> None:
    client: Redis = Redis(unix_socket_path=redis_socket)
    await client.flushall()
    spy = _SpyRedis(client, MarketIpcConfig().stream_name)
    stack, service = _build(spy, _Provider([_tick("A"), _tick("B"), _tick("C")]), str(tmp_path))
    await service.start()
    await service.wait()
    await service.stop()

    assert spy.aclose_calls == 1  # closed exactly once on a clean shutdown
    assert spy.xlen_at_first_close == 3  # M2 drained BEFORE the client was closed (§5)
    assert stack.redis is spy  # the stack owns the one client; no second owner


async def test_repeated_stop_closes_redis_once(redis_socket, tmp_path) -> None:
    client: Redis = Redis(unix_socket_path=redis_socket)
    await client.flushall()
    spy = _SpyRedis(client, MarketIpcConfig().stream_name)
    _stack, service = _build(spy, _Provider([_tick()]), str(tmp_path))
    await service.start()
    await service.wait()
    await service.stop()
    await service.stop()  # idempotent — must not double-close
    await service.stop()
    assert spy.aclose_calls == 1


async def test_startup_failure_closes_redis(redis_socket, tmp_path) -> None:
    client: Redis = Redis(unix_socket_path=redis_socket)
    await client.flushall()
    spy = _SpyRedis(client, MarketIpcConfig().stream_name)
    _stack, service = _build(spy, _UnhealthyProvider([_tick()]), str(tmp_path))
    with pytest.raises(ProviderInitializationError):
        await service.start()
    assert service.status is ServiceStatus.FAILED
    # the entrypoint always stops a failed start → the owned client is closed
    await service.stop()
    assert spy.aclose_calls == 1


async def test_terminal_failure_closes_redis(tmp_path) -> None:
    server = redislite.Redis()  # a dedicated server we can kill mid-publication
    client: Redis = Redis(unix_socket_path=server.socket_file)
    await client.flushall()
    spy = _SpyRedis(client, MarketIpcConfig().stream_name)
    gate = asyncio.Event()
    provider = _GatedProvider([_tick("A")], gate, [_tick("B")])
    _stack, service = _build(spy, provider, str(tmp_path))
    try:
        await service.start()
        await _until(lambda: service.diagnostics().published_total >= 1)  # A landed
        server.shutdown()  # the outage begins
        gate.set()  # B's transmit hits a dead Redis → terminal
        await asyncio.wait_for(service._watch_task, timeout=30.0)  # generous for a loaded CI runner
        assert service.terminal_failure is True
        await service.stop()
        assert spy.aclose_calls == 1  # a terminal incarnation still closes its client
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


# --------------------------------------------------------------------------- #
# Dynamic trading date across a session boundary (§8-§13, §23)
# --------------------------------------------------------------------------- #
async def test_trading_date_rollover_without_restart(redis_socket, tmp_path) -> None:
    client: Redis = Redis(unix_socket_path=redis_socket)
    await client.flushall()
    verify: Redis = Redis(unix_socket_path=redis_socket)
    clock = _Clock(datetime(2026, 9, 14, 6, 0, tzinfo=UTC))  # 09-14 11:30 IST (D1 session)
    gate = asyncio.Event()
    _stack, service = _build(client, _GatedRefProvider(gate), str(tmp_path), now=clock.now)
    d1, d2 = date(2026, 9, 14), date(2026, 9, 15)
    config = MarketIpcConfig()
    try:
        await service.start()
        await _until(lambda: service.diagnostics().published_total >= 1)  # ref#1 committed on D1
        clock.moment = datetime(2026, 9, 14, 19, 0, tzinfo=UTC)  # 09-15 00:30 IST → date rolls
        gate.set()
        await service.wait()  # ref#2 submitted (stamped D2), episode ends
        await service.stop()  # drains ref#2 → the D2 reference key is written

        assert await verify.hget(_ref_key(config, d1), "NSE:TCS") is not None
        assert await verify.hget(_ref_key(config, d2), "NSE:TCS") is not None
        assert service.diagnostics().producer_epoch == 1  # a single incarnation — no restart
    finally:
        with contextlib.suppress(Exception):
            await verify.aclose()


async def test_reconnect_preserves_epoch_across_date_rollover(redis_socket, tmp_path) -> None:
    client: Redis = Redis(unix_socket_path=redis_socket)
    await client.flushall()
    verify: Redis = Redis(unix_socket_path=redis_socket)
    clock = _Clock(datetime(2026, 9, 14, 6, 0, tzinfo=UTC))  # D1 session
    gate = asyncio.Event()
    provider = _DropThenGatedRefProvider(gate)
    _stack, service = _build(client, provider, str(tmp_path), now=clock.now, max_reconnects=1)
    d1, d2 = date(2026, 9, 14), date(2026, 9, 15)
    config = MarketIpcConfig()
    try:
        await service.start()
        await _until(lambda: service.diagnostics().published_total >= 1)  # ref on D1 (episode 1)
        clock.moment = datetime(2026, 9, 14, 19, 0, tzinfo=UTC)  # 09-15 → the date rolls
        gate.set()  # the reconnect episode yields the D2 ref
        await service.wait()
        await service.stop()

        assert provider.connect_calls == 1  # a transport reconnect never re-runs the coordinator
        assert service.diagnostics().producer_epoch == 1  # rollover never allocates a new epoch
        assert await verify.hget(_ref_key(config, d1), "NSE:TCS") is not None
        assert await verify.hget(_ref_key(config, d2), "NSE:TCS") is not None
    finally:
        with contextlib.suppress(Exception):
            await verify.aclose()


# --------------------------------------------------------------------------- #
# Long-lived resource stability (§21, §22)
# --------------------------------------------------------------------------- #
async def test_reconnect_soak_is_stable(redis_socket, tmp_path) -> None:
    client: Redis = Redis(unix_socket_path=redis_socket)
    await client.flushall()
    drops = 60
    provider = _DropNProvider(drops)
    stack, service = _build(client, provider, str(tmp_path), max_reconnects=drops)
    await service.start()
    await service.wait()
    await service.stop()

    diagnostics = service.diagnostics()
    assert diagnostics.reconnect_total == drops  # every cycle self-healed
    assert provider.connect_calls == 1  # coordinator/auth never re-run across the soak
    assert diagnostics.producer_epoch == 1  # one epoch for the whole incarnation
    assert service.terminal_failure is False
    assert await client.xlen(MarketIpcConfig().stream_name) == drops + 1  # every episode's tick
    assert stack.boundary.diagnostics().worker_running is False  # single worker, cleanly stopped
    assert stack.redis is client  # no Redis client accumulation across reconnects
    await client.aclose()


async def test_thousands_of_events_stay_bounded_and_publish(redis_socket, tmp_path) -> None:
    client: Redis = Redis(unix_socket_path=redis_socket)
    await client.flushall()
    count = 2000
    config = MarketIpcConfig()
    stack, service = _build(client, _ManyTicksProvider(count), str(tmp_path), config=config)
    await service.start()
    await service.wait()
    await service.stop()

    boundary = stack.boundary.diagnostics()
    assert await client.xlen(config.stream_name) == count  # every event published
    assert boundary.published_total == count
    assert boundary.overflow_total == 0  # bounded queue never overflowed
    assert boundary.queue_high_watermark <= config.publish_queue_capacity  # stayed within capacity
    assert boundary.worker_running is False  # one worker, cleanly stopped
    await client.aclose()
