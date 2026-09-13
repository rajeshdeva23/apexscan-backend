"""Real disposable-Redis integration for the H4A consumer runtime (DECOUPLING PHASE H4A).

Runs a real ``redis-server`` (bundled by ``redislite`` on a private unix socket) — never a
shared or production Redis. Proves the runtime *composition* over actual stream primitives: the
poll loop applies -> durably marks -> ACKs, a re-published identity is suppressed by the durable
authority even for a FRESH runtime/process, startup fails closed and closes its owned client when
Redis is unreachable, a blocked XREADGROUP shuts down promptly on stop, the owned Redis client is
closed exactly once, no background task leaks, and a bounded soak of mixed duplicates / epochs /
legal gaps converges to the exact apply and duplicate counts. Skips cleanly if redislite is absent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis

from app.market_ingestion.mode import MarketPathMode, PhaseHFlags
from app.market_ipc import (
    MarketEventConsumerRuntime,
    MarketEventEnvelope,
    MarketIpcConfig,
    RecordingShadowSink,
    RedisMarketEventStream,
    RuntimeState,
    build_envelope,
    compose_consumer_runtime,
)
from app.schemas.market_data import (
    FeedContinuity,
    FeedContinuityEvent,
    Instrument,
    MarketReference,
    Quote,
    Tick,
)

redislite = pytest.importorskip("redislite", reason="disposable real Redis unavailable")

_NOW = datetime(2026, 9, 9, 10, 15, 30, tzinfo=UTC)
_TD = date(2026, 9, 9)
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


class _Settings:
    """Minimal settings surface compose needs: redis_url + the two builder methods."""

    def __init__(self, socket: str, *, flags: PhaseHFlags, block_ms: int = 0) -> None:
        self.redis_url = f"unix://{socket}"
        self._flags = flags
        self._block_ms = block_ms

    def phase_h_flags(self) -> PhaseHFlags:
        return self._flags

    def market_ipc_config(self) -> MarketIpcConfig:
        return MarketIpcConfig(block_ms=self._block_ms)


def _shadow_flags() -> PhaseHFlags:
    """The one legal H4A composition: SHADOW_CONSUME_COMPARE (consumer + shadow, no authority)."""
    return PhaseHFlags(
        market_ingestion_service_enabled=False,
        ipc_publisher_enabled=False,
        ipc_consumer_enabled=True,
        ipc_shadow_compare_enabled=True,
        ipc_authoritative_enabled=False,
        legacy_market_path_enabled=True,
    )


def _tick(symbol: str = "TCS", price: str = "100.5") -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        event_timestamp=_NOW,
        last_price=Decimal(price),
    )


def _envelope(payload: object, *, seq: int, epoch: int = 1) -> MarketEventEnvelope:
    return build_envelope(
        payload,
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=7,
    )


async def _compose(
    socket: str,
    *,
    sink: RecordingShadowSink | None = None,
    block_ms: int = 0,
    universe: int | None = 7,
) -> MarketEventConsumerRuntime:
    """Compose a shadow runtime wired with fixture trading-date/universe authorities."""
    return await compose_consumer_runtime(
        _Settings(socket, flags=_shadow_flags(), block_ms=block_ms),
        sink=sink or RecordingShadowSink(),
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: universe,
        now=lambda: _NOW,
    )


async def _wait_until(predicate: Callable[[], bool], *, limit: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached before timeout")


# --------------------------------------------------------------------------- #
# T05: the runtime loop applies -> durably marks -> ACKs, end to end
# --------------------------------------------------------------------------- #
async def test_runtime_applies_marks_and_acks_end_to_end(redis: Redis, redis_socket: str) -> None:
    config = MarketIpcConfig(block_ms=0)
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    for i in range(1, 6):
        await producer.publish(_envelope(_tick(), seq=i))

    sink = RecordingShadowSink()
    runtime = await _compose(redis_socket, sink=sink)
    assert runtime.enabled and runtime.mode is MarketPathMode.SHADOW_CONSUME_COMPARE
    await runtime.start()
    assert runtime.is_ready
    try:
        await _wait_until(lambda: runtime.diagnostics().acked_total >= 5)
    finally:
        await runtime.stop()

    assert sink.applied_total == 5
    assert runtime.diagnostics().applied_total == 5
    assert runtime.state is RuntimeState.STOPPED
    pending = await redis.xpending(config.stream_name, config.consumer_group)
    assert pending["pending"] == 0  # every applied entry was ACKed


# --------------------------------------------------------------------------- #
# T06: a re-published identity is suppressed (no reapply) and still ACKed
# --------------------------------------------------------------------------- #
async def test_duplicate_identity_is_not_reapplied(redis: Redis, redis_socket: str) -> None:
    config = MarketIpcConfig(block_ms=0)
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(_tick(), seq=1))

    sink = RecordingShadowSink()
    runtime = await _compose(redis_socket, sink=sink)
    await runtime.start()
    try:
        await _wait_until(lambda: runtime.diagnostics().applied_total == 1)
        await producer.publish(_envelope(_tick(), seq=1))  # same identity, new stream id
        await _wait_until(lambda: runtime.diagnostics().duplicate_total == 1)
    finally:
        await runtime.stop()

    assert sink.applied_total == 1  # durable dedup suppressed the reapply
    assert runtime.diagnostics().duplicate_total == 1


# --------------------------------------------------------------------------- #
# T07 / T26: durable idempotency survives a full runtime (process) restart
# --------------------------------------------------------------------------- #
async def test_duplicate_survives_runtime_restart(redis: Redis, redis_socket: str) -> None:
    config = MarketIpcConfig(block_ms=0)
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(_tick(), seq=1))

    first = await _compose(redis_socket, sink=RecordingShadowSink())
    await first.start()
    try:
        await _wait_until(lambda: first.diagnostics().applied_total == 1)
    finally:
        await first.stop()  # closes the first runtime's owned Redis client (simulated restart)

    await producer.publish(_envelope(_tick(), seq=1))  # re-publish after "restart"
    second_sink = RecordingShadowSink()
    second = await _compose(redis_socket, sink=second_sink)  # fresh client + empty memory cache
    await second.start()
    try:
        await _wait_until(lambda: second.diagnostics().duplicate_total == 1)
    finally:
        await second.stop()

    assert second_sink.applied_total == 0  # recognised via the DURABLE authority, not memory
    assert second.diagnostics().applied_total == 0


# --------------------------------------------------------------------------- #
# T08 / T23: same sequence under a new epoch is a distinct event (both apply)
# --------------------------------------------------------------------------- #
async def test_same_sequence_new_epoch_both_apply(redis: Redis, redis_socket: str) -> None:
    config = MarketIpcConfig(block_ms=0)
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(_tick(), seq=1, epoch=10))
    await producer.publish(_envelope(_tick(), seq=1, epoch=11))

    runtime = await _compose(redis_socket, sink=RecordingShadowSink())
    await runtime.start()
    try:
        await _wait_until(lambda: runtime.diagnostics().applied_total == 2)
    finally:
        await runtime.stop()

    assert runtime.diagnostics().duplicate_total == 0  # new epoch is a new incarnation


# --------------------------------------------------------------------------- #
# T25: every canonical published event class decodes and applies consistently
# --------------------------------------------------------------------------- #
async def test_multiple_canonical_event_types_apply(redis: Redis, redis_socket: str) -> None:
    config = MarketIpcConfig(block_ms=0)
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    instrument = Instrument(exchange="NSE", symbol="TCS")
    payloads = [
        _tick(),
        Quote(
            instrument=instrument,
            event_timestamp=_NOW,
            bid_price=Decimal("100.4"),
            ask_price=Decimal("100.6"),
            bid_quantity=10,
            ask_quantity=12,
        ),
        MarketReference(instrument=instrument, previous_close=Decimal("99.0")),
        FeedContinuityEvent(status=FeedContinuity.CONNECTED, observed_at=_NOW),
    ]
    for seq, payload in enumerate(payloads, start=1):
        await producer.publish(_envelope(payload, seq=seq))

    runtime = await _compose(redis_socket, sink=RecordingShadowSink())
    await runtime.start()
    try:
        await _wait_until(lambda: runtime.diagnostics().applied_total == len(payloads))
    finally:
        await runtime.stop()

    assert runtime.diagnostics().payload_decode_failures == 0
    assert runtime.diagnostics().event_kind_mismatch_total == 0


# --------------------------------------------------------------------------- #
# T15: startup fails closed and closes the owned client when Redis is unreachable
# --------------------------------------------------------------------------- #
async def test_startup_redis_failure_is_fail_closed_and_cleans_up() -> None:
    runtime = await compose_consumer_runtime(
        _Settings("/nonexistent/apexscan-h4a.sock", flags=_shadow_flags()),
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: 7,
        now=lambda: _NOW,
    )
    with pytest.raises(Exception):  # noqa: B017,PT011 - ensure_group surfaces the Redis failure
        await runtime.start()
    assert runtime.state is RuntimeState.FAILED  # never claimed READY
    assert not runtime.is_ready
    assert runtime._task is None  # no partial background task left running  # noqa: SLF001
    await runtime.stop()  # idempotent: safe after a failed start


# --------------------------------------------------------------------------- #
# T16: a blocked XREADGROUP wakes and shuts down promptly on stop
# --------------------------------------------------------------------------- #
async def test_blocked_read_shuts_down_promptly(redis_socket: str) -> None:
    runtime = await _compose(redis_socket, block_ms=5_000)  # loop long-polls on an empty stream
    await runtime.start()
    assert runtime.is_ready
    loop = asyncio.get_running_loop()
    start = loop.time()
    await runtime.stop()  # must cancel the blocked read rather than wait out the 5s block
    elapsed = loop.time() - start

    assert elapsed < 2.0  # cancellation woke the blocked read
    assert runtime.state is RuntimeState.STOPPED
    assert runtime._task is None  # no leaked task  # noqa: SLF001


# --------------------------------------------------------------------------- #
# T17 / T18 / T19: shutdown mid-batch closes the client exactly once, no leak
# --------------------------------------------------------------------------- #
async def test_shutdown_closes_client_once_and_leaves_no_task(
    redis: Redis, redis_socket: str
) -> None:
    config = MarketIpcConfig(block_ms=0)
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    for i in range(1, 51):
        await producer.publish(_envelope(_tick(), seq=i))

    runtime = await _compose(redis_socket, sink=RecordingShadowSink())
    closes = {"count": 0}
    original_close = runtime._redis.aclose  # noqa: SLF001

    async def _counting_close() -> None:
        closes["count"] += 1
        await original_close()

    runtime._redis.aclose = _counting_close  # type: ignore[method-assign]  # noqa: SLF001
    await runtime.start()
    await asyncio.sleep(0.03)  # let a batch be in flight
    await runtime.stop()
    await runtime.stop()  # second stop must be a no-op for the client

    assert closes["count"] == 1  # Redis client closed exactly once
    assert runtime._task is None  # no leaked task  # noqa: SLF001
    assert runtime.state is RuntimeState.STOPPED


# --------------------------------------------------------------------------- #
# T39 / T09: bounded soak — duplicates + epochs + legal gaps converge exactly
# --------------------------------------------------------------------------- #
async def test_bounded_soak_converges_to_exact_counts(redis: Redis, redis_socket: str) -> None:
    config = MarketIpcConfig(block_ms=0, read_count=200)
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()

    unique: set[tuple[int, int]] = set()  # (epoch, sequence)
    published = 0
    for epoch in (1, 2):  # two producer incarnations
        seq = 0
        for _ in range(800):
            seq += 2  # legal sequence gaps (seq allocated before enqueue; +2 each step)
            await producer.publish(_envelope(_tick(), seq=seq, epoch=epoch))
            unique.add((epoch, seq))
            published += 1
    replays = list(unique)[:600]
    for epoch, seq in replays:  # replay 600 identities (duplicates)
        await producer.publish(_envelope(_tick(), seq=seq, epoch=epoch))
        published += 1
    assert published >= 2_000

    sink = RecordingShadowSink(max_entries=5_000)
    runtime = await compose_consumer_runtime(
        _Settings(redis_socket, flags=_shadow_flags(), block_ms=0),
        sink=sink,
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: 7,
        now=lambda: _NOW,
    )
    await runtime.start()
    try:
        await _wait_until(lambda: runtime.diagnostics().acked_total >= published, limit=20.0)
    finally:
        await runtime.stop()

    diag = runtime.diagnostics()
    assert diag.applied_total == len(unique)  # each unique identity applied exactly once
    assert diag.duplicate_total == len(replays)  # every replay suppressed
    assert diag.acked_total == published  # everything safe was ACKed
    assert runtime._task is None  # single bounded task, torn down  # noqa: SLF001
    pending = await redis.xpending(config.stream_name, config.consumer_group)
    assert pending["pending"] == 0
