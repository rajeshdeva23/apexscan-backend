"""Real disposable-Redis integration for H4B pending-entry recovery (DECOUPLING PHASE H4B).

Runs a real ``redis-server`` (bundled by ``redislite`` on a private unix socket) — never a
shared or production Redis. Proves that entries stranded in the Redis consumer-group PEL are
reclaimed by XAUTOCLAIM and reprocessed through the SAME C1 correctness gate: an ACK-lost entry is
recognised as a durable duplicate and not reapplied; a transiently-failed entry stays pending and
retries; the apply->mark crash (B2) window is demonstrated honestly (a reclaim can reapply);
below-idle entries are protected; abandoned-consumer entries are reclaimed by another consumer;
failures at claim/dedup/mark/ack never fabricate success; legal sequence gaps and new epochs are
respected; and a bounded soak converges. Skips cleanly if redislite is absent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.market_ingestion.mode import PhaseHFlags
from app.market_ipc import (
    BoundedDeduplicator,
    CompositeDeduplicator,
    DurableDeduplicator,
    MarketEventConsumer,
    MarketEventConsumerRuntime,
    MarketEventEnvelope,
    MarketIpcConfig,
    RecordingShadowSink,
    RedisMarketEventStream,
    build_envelope,
    compose_consumer_runtime,
    dedup_key,
)
from app.market_ipc.envelope import ProducerEventIdentity
from app.schemas.market_data import Instrument, Tick

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


def _tick(symbol: str = "TCS", price: str = "100.5") -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol=symbol),
        event_timestamp=_NOW,
        last_price=Decimal(price),
    )


def _envelope(*, seq: int, epoch: int = 1, symbol: str = "TCS") -> MarketEventEnvelope:
    return build_envelope(
        _tick(symbol),
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=7,
    )


def _config(**overrides: object) -> MarketIpcConfig:
    return MarketIpcConfig(block_ms=0, **overrides)


def _named(redis: Redis, config: MarketIpcConfig, name: str) -> RedisMarketEventStream:
    """A stream bound to a distinct consumer name in the same group (a separate incarnation)."""
    return RedisMarketEventStream(
        redis=redis, config=config.model_copy(update={"consumer_name": name})
    )


def _fast_idle(config: MarketIpcConfig, *, consumer_name: str) -> MarketIpcConfig:
    # model_copy(update=...) intentionally bypasses the ge=1000 floor for a fast test idle window,
    # matching the existing H4A recovery tests; production calibration is out of scope (§6).
    return config.model_copy(update={"consumer_name": consumer_name, "claim_idle_ms": 1})


def _durable(redis: Redis, config: MarketIpcConfig) -> CompositeDeduplicator:
    return CompositeDeduplicator(
        memory=BoundedDeduplicator(config.dedup_max_entries),
        durable=DurableDeduplicator(redis, config),
    )


def _consumer(
    redis: Redis,
    config: MarketIpcConfig,
    sink: RecordingShadowSink,
    *,
    transport: RedisMarketEventStream | None = None,
    deduplicator: CompositeDeduplicator | None = None,
) -> MarketEventConsumer:
    return MarketEventConsumer(
        transport=transport or RedisMarketEventStream(redis=redis, config=config),
        config=config,
        sink=sink,
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: 7,
        now=lambda: _NOW,
        deduplicator=deduplicator or _durable(redis, config),
    )


async def _pending(redis: Redis, config: MarketIpcConfig) -> int:
    summary = await redis.xpending(config.stream_name, config.consumer_group)
    return int(summary["pending"])


async def _wait_until(predicate: Callable[[], bool], *, limit: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached before timeout")


# --------------------------------------------------------------------------- #
# Test doubles: inject one failure mode while every other primitive stays real.
# --------------------------------------------------------------------------- #
class _FailOnceSink(RecordingShadowSink):
    """Shadow sink that raises on its first apply, then records normally (transient fault)."""

    def __init__(self) -> None:
        super().__init__()
        self._failed = False

    async def apply(self, envelope: MarketEventEnvelope, event: object) -> None:  # type: ignore[override]
        if not self._failed:
            self._failed = True
            raise RuntimeError("transient sink failure")
        await super().apply(envelope, event)  # type: ignore[arg-type]


class _RecordCrashDedup:
    """Dedup whose durable ``record`` raises (crash before the C1 mark commits); contains real."""

    def __init__(self, delegate: CompositeDeduplicator) -> None:
        self._delegate = delegate

    async def contains(self, identity: ProducerEventIdentity) -> bool:
        return await self._delegate.contains(identity)

    async def record(self, identity: ProducerEventIdentity) -> None:
        raise RedisError("simulated crash before durable mark")


class _ContainsCrashDedup:
    """Dedup whose durable ``contains`` raises (store unavailable during recovery)."""

    def __init__(self, delegate: CompositeDeduplicator) -> None:
        self._delegate = delegate

    async def contains(self, identity: ProducerEventIdentity) -> bool:
        raise RedisError("simulated dedup store outage")

    async def record(self, identity: ProducerEventIdentity) -> None:
        await self._delegate.record(identity)


class _AckCrashTransport:
    """Transport delegate whose ``ack`` raises; every other primitive is the real stream."""

    def __init__(self, delegate: RedisMarketEventStream) -> None:
        self._delegate = delegate

    async def ensure_group(self) -> None:
        await self._delegate.ensure_group()

    async def read_raw(self) -> list[tuple[str, bytes | None]]:
        return await self._delegate.read_raw()

    async def claim_page_raw(self, start_id: str) -> tuple[str, list[tuple[str, bytes | None]]]:
        return await self._delegate.claim_page_raw(start_id)

    async def ack(self, *message_ids: str) -> int:
        raise RedisError("simulated ACK failure")


# =========================================================================== #
# T02 / T28: no pending -> bounded no-op recovery pass
# =========================================================================== #
async def test_no_pending_bounded_noop_recovery(redis: Redis) -> None:
    config = _config()
    await RedisMarketEventStream(redis=redis, config=config).ensure_group()
    consumer = _consumer(redis, config, RecordingShadowSink())
    await consumer.start()
    await consumer.poll_once()

    diag = consumer.diagnostics()
    assert diag.pending_recovery_runs == 1  # one bounded recovery pass ran
    assert diag.pending_reclaimed == 0
    assert diag.pending_reclaim_failures == 0
    assert await _pending(redis, config) == 0


# =========================================================================== #
# T03 / T28: an abandoned entry is reclaimed once idle, then PEL drains
# =========================================================================== #
async def test_abandoned_entry_reclaimed_after_idle(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(seq=1))

    dead = _named(redis, config, "dead")
    await dead.read_raw()  # E1 now pending under "dead", never acked
    assert await _pending(redis, config) == 1

    sink = RecordingShadowSink()
    consumer = _consumer(redis, _fast_idle(config, consumer_name="recover"), sink)
    await consumer.start()
    await asyncio.sleep(0.02)  # exceed the 1ms idle threshold
    await consumer.poll_once()

    assert sink.applied_total == 1
    assert consumer.diagnostics().pending_reclaimed_applied == 1
    assert await _pending(redis, config) == 0  # PEL drained after successful recovery


# =========================================================================== #
# T04: an entry still within the idle window is NOT stolen
# =========================================================================== #
async def test_below_idle_entry_not_stolen(redis: Redis) -> None:
    config = _config()  # default claim_idle_ms 30_000
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(seq=1))
    dead = _named(redis, config, "dead")
    await dead.read_raw()

    sink = RecordingShadowSink()
    consumer = _consumer(redis, config.model_copy(update={"consumer_name": "recover"}), sink)
    await consumer.start()
    await consumer.poll_once()  # no sleep: the entry is far below the 30s idle threshold

    assert sink.applied_total == 0  # not stolen from the (nominally live) owner
    assert await _pending(redis, config) == 1  # still owned by "dead"


# =========================================================================== #
# T05: a pending backlog larger than the claim page recovers across bounded cycles
# =========================================================================== #
async def test_multipage_pending_recovers_across_bounded_cycles(redis: Redis) -> None:
    base = _config(read_count=100)
    producer = RedisMarketEventStream(redis=redis, config=base)
    await producer.ensure_group()
    for i in range(1, 251):
        await producer.publish(_envelope(seq=i))
    dead = _named(redis, base, "dead")
    while await dead.read_raw():  # strand all 250 in the dead consumer's PEL
        pass

    sink = RecordingShadowSink(max_entries=500)
    consumer = _consumer(redis, _fast_idle(base, consumer_name="recover"), sink)
    await consumer.start()
    await asyncio.sleep(0.02)
    per_cycle: list[int] = []
    for _ in range(6):
        before = consumer.diagnostics().pending_reclaimed_applied
        await consumer.poll_once()
        per_cycle.append(consumer.diagnostics().pending_reclaimed_applied - before)
        if consumer.diagnostics().pending_reclaimed_applied >= 250:
            break

    assert consumer.diagnostics().pending_reclaimed_applied == 250
    assert max(per_cycle) <= 100  # bounded per cycle (no unbounded single-cycle drain)
    assert len([c for c in per_cycle if c > 0]) >= 3  # genuinely multi-page
    assert await _pending(redis, base) == 0


# =========================================================================== #
# T06 / T14(crash): Consumer B reclaims a dead Consumer A's entry and processes it
# =========================================================================== #
async def test_consumer_b_reclaims_dead_consumer_a(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(seq=1))
    a = _named(redis, config, "A")
    await a.read_raw()  # A receives, then "disappears" (no ack, no further calls)

    sink = RecordingShadowSink()
    b = _consumer(redis, _fast_idle(config, consumer_name="B"), sink)
    await b.start()
    await asyncio.sleep(0.02)
    await b.poll_once()

    assert sink.applied_total == 1  # ownership moved A -> B and B processed it
    assert await _pending(redis, config) == 0


# =========================================================================== #
# T07: a reclaimed, never-processed entry applies -> marks -> ACKs
# =========================================================================== #
async def test_reclaimed_unprocessed_applies_marks_acks(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(seq=1))
    dead = _named(redis, config, "dead")
    await dead.read_raw()

    recover_config = _fast_idle(config, consumer_name="recover")
    sink = RecordingShadowSink()
    consumer = _consumer(redis, recover_config, sink)
    await consumer.start()
    await asyncio.sleep(0.02)
    await consumer.poll_once()

    assert sink.applied_total == 1
    identity = ProducerEventIdentity(_PRODUCER, 1, 1)
    assert await redis.exists(dedup_key(config.dedup_key_prefix, identity)) == 1  # durably marked
    assert await _pending(redis, config) == 0


# =========================================================================== #
# T08 / T26(ack-lost): applied + durably marked but ACK lost -> reclaim is a duplicate
# =========================================================================== #
async def test_ack_lost_reclaim_is_duplicate_not_reapplied(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(seq=1))

    sink = RecordingShadowSink()
    # Consumer A: real apply + real durable mark, but its ACK always fails -> entry stays pending.
    a_config = config.model_copy(update={"consumer_name": "A"})
    a = _consumer(
        redis,
        a_config,
        sink,
        transport=_AckCrashTransport(RedisMarketEventStream(redis=redis, config=a_config)),  # type: ignore[arg-type]
    )
    await a.start()
    await a.poll_once()
    assert sink.applied_total == 1
    assert a.diagnostics().ack_failures == 1
    assert await _pending(redis, config) == 1  # applied + marked, but not acked

    # Consumer B reclaims after idle: durable dedup recognises it -> no reapply -> ACK. B shares
    # A's sink, so a spurious reapply would push applied_total to 2 and fail this assertion.
    b = _consumer(redis, _fast_idle(config, consumer_name="B"), sink)
    await b.start()
    await asyncio.sleep(0.02)
    await b.poll_once()

    assert sink.applied_total == 1  # NOT reapplied (durable authority across incarnations)
    assert b.diagnostics().pending_reclaimed_duplicates == 1
    assert await _pending(redis, config) == 0


# =========================================================================== #
# T11: apply -> (crash before mark) -> reclaim reapplies — the B2 window, unhidden
# =========================================================================== #
async def test_apply_then_mark_crash_reclaim_reapplies_b2_window(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(seq=1))

    sink = RecordingShadowSink()
    # Consumer A: sink apply succeeds, durable mark raises (crash), no ACK -> pending, NOT marked.
    a_config = config.model_copy(update={"consumer_name": "A"})
    a = _consumer(redis, a_config, sink, deduplicator=_RecordCrashDedup(_durable(redis, a_config)))  # type: ignore[arg-type]
    await a.start()
    await a.poll_once()
    assert sink.applied_total == 1
    assert a.diagnostics().dedup_store_failures == 1
    identity = ProducerEventIdentity(_PRODUCER, 1, 1)
    assert await redis.exists(dedup_key(config.dedup_key_prefix, identity)) == 0  # never marked

    # Consumer B reclaims: durable contains is false -> it reapplies (duplicate application).
    b = _consumer(redis, _fast_idle(config, consumer_name="B"), sink)
    await b.start()
    await asyncio.sleep(0.02)
    await b.poll_once()

    assert sink.applied_total == 2  # B2: apply happened twice — expected until B2 is implemented
    assert b.diagnostics().pending_reclaimed_applied == 1
    assert await _pending(redis, config) == 0


# =========================================================================== #
# T09 / T27: a transiently sink-failed pending entry is retried, never poison-discarded
# =========================================================================== #
async def test_sink_failed_pending_retries_on_recovery(redis: Redis) -> None:
    config = _fast_idle(_config(), consumer_name="backend-0")
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(seq=1))

    sink = _FailOnceSink()
    consumer = _consumer(redis, config, sink)
    await consumer.start()
    await consumer.poll_once()  # first delivery: sink raises -> pending, not acked
    assert sink.applied_total == 0
    assert consumer.diagnostics().sink_failures == 1
    assert await _pending(redis, config) == 1  # transient failure is NOT terminally discarded

    await asyncio.sleep(0.02)
    await consumer.poll_once()  # reclaim + retry: sink now succeeds
    assert sink.applied_total == 1
    assert await _pending(redis, config) == 0


# =========================================================================== #
# T13: a dedup-store failure during recovery is fail-closed (entry left pending)
# =========================================================================== #
async def test_dedup_failure_during_recovery_is_fail_closed(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(seq=1))
    dead = _named(redis, config, "dead")
    await dead.read_raw()

    recover_config = _fast_idle(config, consumer_name="recover")
    sink = RecordingShadowSink()
    consumer = _consumer(
        redis,
        recover_config,
        sink,
        deduplicator=_ContainsCrashDedup(_durable(redis, recover_config)),  # type: ignore[arg-type]
    )
    await consumer.start()
    await asyncio.sleep(0.02)
    await consumer.poll_once()

    assert sink.applied_total == 0  # never applied without idempotency protection
    assert consumer.diagnostics().dedup_store_failures == 1
    assert await _pending(redis, config) == 1  # left pending for a later recovery pass


# =========================================================================== #
# T14: an ACK failure during recovery leaves the entry pending
# =========================================================================== #
async def test_ack_failure_during_recovery_leaves_pending(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(seq=1))
    dead = _named(redis, config, "dead")
    await dead.read_raw()

    recover_config = _fast_idle(config, consumer_name="recover")
    sink = RecordingShadowSink()
    consumer = _consumer(
        redis,
        recover_config,
        sink,
        transport=_AckCrashTransport(RedisMarketEventStream(redis=redis, config=recover_config)),  # type: ignore[arg-type]
    )
    await consumer.start()
    await asyncio.sleep(0.02)
    await consumer.poll_once()

    assert sink.applied_total == 1  # applied + durably marked
    assert consumer.diagnostics().ack_failures == 1
    assert await _pending(redis, config) == 1  # ACK failed -> still pending (redelivers)


# =========================================================================== #
# T12: a claim (XAUTOCLAIM) Redis failure is visible and never fabricates an ACK
# =========================================================================== #
async def test_claim_redis_failure_is_visible_no_false_ack() -> None:
    config = _config()
    broken = Redis(unix_socket_path="/nonexistent/apexscan-h4b.sock")
    sink = RecordingShadowSink()
    try:
        consumer = _consumer(broken, config, sink)
        await consumer.poll_once()  # must not raise

        diag = consumer.diagnostics()
        assert diag.pending_reclaim_failures == 1  # recovery-scoped visibility
        assert diag.read_failures == 1  # generic read-failure counter preserved (H4A contract)
        assert diag.acked_total == 0  # no false ACK
        assert sink.applied_total == 0
    finally:
        await broken.aclose()


# =========================================================================== #
# T15 / T23: same sequence under a new epoch stays distinct on recovery
# =========================================================================== #
async def test_same_sequence_new_epoch_distinct_on_recovery(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(seq=1, epoch=10))
    await producer.publish(_envelope(seq=1, epoch=11))
    dead = _named(redis, config, "dead")
    while await dead.read_raw():
        pass

    sink = RecordingShadowSink()
    consumer = _consumer(redis, _fast_idle(config, consumer_name="recover"), sink)
    await consumer.start()
    await asyncio.sleep(0.02)
    await consumer.poll_once()

    assert sink.applied_total == 2  # epoch 10/seq 1 and epoch 11/seq 1 are distinct incarnations
    assert consumer.diagnostics().pending_reclaimed_duplicates == 0


# =========================================================================== #
# T16 / T22(spec): legal producer sequence gaps are tolerated on recovery
# =========================================================================== #
async def test_legal_sequence_gaps_tolerated_on_recovery(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    for seq in (100, 102, 101):  # a gap then out-of-order; NOT loss
        await producer.publish(_envelope(seq=seq))
    dead = _named(redis, config, "dead")
    while await dead.read_raw():
        pass

    sink = RecordingShadowSink()
    consumer = _consumer(redis, _fast_idle(config, consumer_name="recover"), sink)
    await consumer.start()
    await asyncio.sleep(0.02)
    await consumer.poll_once()

    assert sink.applied_total == 3  # no sequence-arithmetic loss inference
    assert await _pending(redis, config) == 0


# =========================================================================== #
# T26: a permanently-invalid poison entry is terminally ACKed on reclaim (H4A contract)
# =========================================================================== #
async def test_permanent_poison_reclaim_is_terminally_acked(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await redis.xadd(config.stream_name, {"e": b"not-a-valid-envelope"})  # permanent poison
    dead = _named(redis, config, "dead")
    await dead.read_raw()

    consumer = _consumer(redis, _fast_idle(config, consumer_name="recover"), RecordingShadowSink())
    await consumer.start()
    await asyncio.sleep(0.02)
    await consumer.poll_once()

    assert consumer.diagnostics().envelope_decode_failures == 1
    assert await _pending(redis, config) == 0  # poison terminally ACKed, never jams the group


# =========================================================================== #
# T17: pending recovery and new-message reads both make progress (no starvation)
# =========================================================================== #
async def test_pending_and_new_no_starvation(redis: Redis) -> None:
    config = _config(read_count=50)
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    for i in range(1, 41):  # 40 stranded (pending)
        await producer.publish(_envelope(seq=i))
    dead = _named(redis, config, "dead")
    while await dead.read_raw():
        pass
    for i in range(41, 81):  # 40 fresh (never delivered)
        await producer.publish(_envelope(seq=i))

    sink = RecordingShadowSink(max_entries=200)
    consumer = _consumer(redis, _fast_idle(config, consumer_name="recover"), sink)
    await consumer.start()
    await asyncio.sleep(0.02)
    await consumer.poll_once()  # ONE cycle: recovery pass + new pass

    diag = consumer.diagnostics()
    assert diag.pending_reclaimed_applied > 0  # recovery progressed
    assert diag.received_total > 0  # new reads progressed in the SAME cycle
    assert sink.applied_total == 80  # both drained together
    assert await _pending(redis, config) == 0


# =========================================================================== #
# Runtime-level: abandoned recovery via the poll loop; close-once, no leak (T18/T21/T22)
# =========================================================================== #
class _Settings:
    def __init__(
        self, socket: str, *, name: str = "backend-0", fast_idle: bool = True, block_ms: int = 0
    ) -> None:
        self.redis_url = f"unix://{socket}"
        self._name = name
        self._fast_idle = fast_idle
        self._block_ms = block_ms

    def phase_h_flags(self) -> PhaseHFlags:
        return PhaseHFlags(
            market_ingestion_service_enabled=False,
            ipc_publisher_enabled=False,
            ipc_consumer_enabled=True,
            ipc_shadow_compare_enabled=True,
            ipc_authoritative_enabled=False,
            legacy_market_path_enabled=True,
        )

    def market_ipc_config(self) -> MarketIpcConfig:
        base = MarketIpcConfig(block_ms=self._block_ms, consumer_name=self._name)
        return base.model_copy(update={"claim_idle_ms": 1}) if self._fast_idle else base


async def _compose_runtime(
    socket: str, sink: RecordingShadowSink, **kw: object
) -> MarketEventConsumerRuntime:
    return await compose_consumer_runtime(
        _Settings(socket, **kw),  # type: ignore[arg-type]
        sink=sink,
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: 7,
        now=lambda: _NOW,
    )


async def test_runtime_loop_recovers_abandoned_pending(redis: Redis, redis_socket: str) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(seq=1))
    dead = _named(redis, config, "dead")
    await dead.read_raw()  # stranded before the runtime starts (recovery-on-startup)

    sink = RecordingShadowSink()
    runtime = await _compose_runtime(redis_socket, sink)
    await runtime.start()
    try:
        await _wait_until(lambda: runtime.diagnostics().pending_reclaimed_applied == 1)
    finally:
        await runtime.stop()

    assert sink.applied_total == 1
    assert runtime._task is None  # no leaked task  # noqa: SLF001
    assert await _pending(redis, config) == 0


async def test_runtime_shutdown_during_recovery_leaves_pending(
    redis: Redis, redis_socket: str
) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    await producer.publish(_envelope(seq=1))
    dead = _named(redis, config, "dead")
    await dead.read_raw()

    # A sink that blocks on first apply lets us stop() mid-recovery; the entry must stay pending.
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingSink(RecordingShadowSink):
        async def apply(self, envelope: MarketEventEnvelope, event: object) -> None:  # type: ignore[override]
            started.set()
            await release.wait()

    sink = _BlockingSink()
    runtime = await _compose_runtime(redis_socket, sink)
    await runtime.start()
    await asyncio.wait_for(started.wait(), timeout=2.0)  # recovery reclaimed and entered the sink
    stop = asyncio.ensure_future(runtime.stop())  # cancels the loop while the sink is mid-apply
    release.set()
    await asyncio.wait_for(stop, timeout=2.0)

    assert runtime._task is None  # noqa: SLF001
    assert (
        await _pending(redis, config) == 1
    )  # unfinished reclaimed entry NOT acked -> still pending


async def test_runtime_blocked_read_and_recovery_shut_down_cleanly(redis_socket: str) -> None:
    # block_ms>0: each cycle runs a (no-op) recovery pass then a genuinely blocking XREADGROUP on
    # an empty stream; stop() must cancel the blocked read promptly. Redis closed exactly once
    # across a double stop().
    settings = _Settings(redis_socket, name="backend-0", fast_idle=False, block_ms=5_000)
    runtime = await compose_consumer_runtime(
        settings,  # type: ignore[arg-type]
        sink=RecordingShadowSink(),
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: 7,
        now=lambda: _NOW,
    )
    await runtime.start()
    closes = {"n": 0}
    original = runtime._redis.aclose  # noqa: SLF001

    async def _counting() -> None:
        closes["n"] += 1
        await original()

    runtime._redis.aclose = _counting  # type: ignore[method-assign]  # noqa: SLF001
    loop = asyncio.get_running_loop()
    start = loop.time()
    await runtime.stop()  # must wake the 5s-blocked read, not wait it out
    assert loop.time() - start < 2.0
    await runtime.stop()
    assert closes["n"] == 1
    assert runtime._task is None  # noqa: SLF001


# =========================================================================== #
# T29: two consumers in the same group — B reclaims A's abandoned entries
# =========================================================================== #
async def test_two_consumers_same_group_reclaim(redis: Redis) -> None:
    config = _config()
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    for i in range(1, 5):
        await producer.publish(_envelope(seq=i))

    sink_a = RecordingShadowSink()
    a = _consumer(redis, config.model_copy(update={"consumer_name": "A"}), sink_a)
    await a.start()
    # A reads a batch but "dies" before acking any of it (simulated by never calling poll again).
    await a._transport.read_raw()  # noqa: SLF001 - deliver to A's PEL without processing
    assert await _pending(redis, config) == 4

    sink_b = RecordingShadowSink()
    b = _consumer(redis, _fast_idle(config, consumer_name="B"), sink_b)
    await b.start()
    await asyncio.sleep(0.02)
    for _ in range(3):
        await b.poll_once()
        if await _pending(redis, config) == 0:
            break

    assert sink_b.applied_total == 4  # B reclaimed and processed all of A's abandoned work
    assert await _pending(redis, config) == 0


# =========================================================================== #
# Soak (§42): normal + abandoned + duplicates + ack-lost + epochs + gaps -> exact counts
# =========================================================================== #
async def test_recovery_soak(redis: Redis) -> None:
    base = _config(read_count=200)
    producer = RedisMarketEventStream(redis=redis, config=base)
    await producer.ensure_group()

    unique: set[tuple[int, int]] = set()
    published = 0
    for epoch in (1, 2):
        seq = 0
        for _ in range(600):
            seq += 2  # legal +2 gaps
            await producer.publish(_envelope(seq=seq, epoch=epoch))
            unique.add((epoch, seq))
            published += 1
    replays = list(unique)[:800]
    for epoch, seq in replays:  # duplicate re-publishes (distinct stream ids, same identity)
        await producer.publish(_envelope(seq=seq, epoch=epoch))
        published += 1
    assert published >= 2_000

    # Strand every entry in a dead consumer's PEL so ALL of it must be recovered by XAUTOCLAIM.
    dead = _named(redis, base, "dead")
    while await dead.read_raw():
        pass
    assert await _pending(redis, base) == published

    sink = RecordingShadowSink(max_entries=5_000)
    recover = _fast_idle(base, consumer_name="recover")
    consumer = _consumer(redis, recover, sink)
    await consumer.start()
    await asyncio.sleep(0.02)
    for _ in range(60):
        await consumer.poll_once()
        if await _pending(redis, base) == 0:
            break

    diag = consumer.diagnostics()
    assert diag.pending_reclaimed_applied == len(unique)  # each unique identity applied once
    assert diag.pending_reclaimed_duplicates == len(replays)  # every replay suppressed
    assert diag.pending_recovery_runs >= 1
    assert await _pending(redis, base) == 0  # PEL fully drained
    assert sink.applied_total == len(unique)
