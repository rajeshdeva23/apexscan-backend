"""B2 closure at the authoritative sink path over real Redis (DECOUPLING PHASE H8A).

The engine-level proofs live in ``tests/unit/market_engine/test_tick_engine_b2_idempotency.py``.
This module proves the SAME closure end-to-end at the consumer -> authoritative-sink -> Redis
boundary against a disposable ``redislite`` server (never a shared/production Redis): the C1
consumer reads ``md:events``, dedups by ``(producer_id, epoch, sequence)``, and applies each
event to a REAL :class:`TickEngine` (the authoritative sink) instead of the compare-only shadow
sink. It then drives the apply->mark crash window (durable mark crashes after a successful engine
apply), the ACK-lost window, and a large duplicate-stress replay, and asserts the engine's
authoritative state is byte-identical to a single clean application.

Crash matrix (§13) exercised here at the real boundary:

* C3 — after engine apply, before the durable C1 mark: reclaim re-delivers the event; the engine's
  DUPLICATE / reference gate makes the re-apply a no-op (T04/T05, and the reference variant T16).
* C4 — after the durable mark, before ACK: C1 ``contains`` suppresses the reclaim so the engine is
  never re-invoked (T06).
* C0/C1/C5 — healthy read/decode/ACK path applies each identity exactly once (baseline).

The sink deliberately keeps a raw-delivery counter so the test proves the duplicate genuinely
reached the sink (non-tautological, §41) while the *authoritative* state was not mutated twice —
the shadow-vs-authoritative distinction the frozen design requires (§15/T21). No production
composition, no live Dhan, no IPC authority: the engine is constructed only inside the test.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.events.bus import EventBus
from app.market_engine.clock import ManualClock
from app.market_engine.state import InstrumentStateRegistry
from app.market_engine.tick_engine import TickEngine
from app.market_ipc import (
    BoundedDeduplicator,
    CompositeDeduplicator,
    DurableDeduplicator,
    MarketEventConsumer,
    MarketEventEnvelope,
    MarketIpcConfig,
    RedisMarketEventStream,
    build_envelope,
)
from app.market_ipc.envelope import ProducerEventIdentity
from app.market_ipc.events import IpcPayload
from app.schemas.market_data import (
    FeedContinuityEvent,
    Instrument,
    MarketReference,
    Quote,
    Tick,
)

redislite = pytest.importorskip("redislite", reason="disposable real Redis unavailable")

_NOW = datetime(2026, 9, 14, 6, 30, tzinfo=UTC)  # 12:00 IST, inside the live session
_CLOCK = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)  # engine clock, ahead of every event ts
_TD = date(2026, 9, 14)
_PRODUCER = "market-ingestion"
_SYMBOLS = ("TCS", "INFY", "RELIANCE")


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


# --------------------------------------------------------------------------- #
# Canonical fixtures (deterministic, tz-aware UTC; per-instrument increasing ts; no FIX-2)
# --------------------------------------------------------------------------- #
def _instrument(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _tick(symbol: str, *, seq: int, price: str) -> Tick:
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=_NOW + timedelta(seconds=seq),
        last_price=Decimal(price),
        traded_quantity=5,
    )


def _quote(symbol: str, *, seq: int, bid: str) -> Quote:
    return Quote(
        instrument=_instrument(symbol),
        event_timestamp=_NOW + timedelta(seconds=seq),
        bid_price=Decimal(bid),
        ask_price=Decimal(str(Decimal(bid) + 2)),
        bid_quantity=1,
        ask_quantity=1,
    )


def _reference(symbol: str, *, previous_close: str) -> MarketReference:
    return MarketReference(instrument=_instrument(symbol), previous_close=Decimal(previous_close))


def _envelope(payload: IpcPayload, *, seq: int, epoch: int = 1) -> MarketEventEnvelope:
    return build_envelope(
        payload,
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=7,
    )


def _distinct_fixture(count: int) -> list[MarketEventEnvelope]:
    """Genuinely-distinct events: each accepted one advances its instrument's context exactly once.

    Models reality so a delayed reclaim can never regress state: one MarketReference per instrument
    up front (``previous_close`` is a per-(instrument, trading-date) constant — set once per
    session, never a stream of distinct values), then high-frequency ticks/quotes with
    per-instrument strictly-increasing timestamps. A reclaimed out-of-order tick/quote is rejected
    STALE by the timestamp watermark; the single reference is held by the value-equality gate.
    """
    envelopes: list[MarketEventEnvelope] = []
    for j, symbol in enumerate(_SYMBOLS, start=1):
        envelopes.append(_envelope(_reference(symbol, previous_close=str(1_000 + j)), seq=j))
    seq = len(_SYMBOLS)
    for i in range(count - len(_SYMBOLS)):
        seq += 1
        symbol = _SYMBOLS[i % len(_SYMBOLS)]
        payload: IpcPayload = (
            _tick(symbol, seq=seq, price=str(100 + seq))
            if i % 2 == 0
            else _quote(symbol, seq=seq, bid=str(90 + seq))
        )
        envelopes.append(_envelope(payload, seq=seq))
    return envelopes


# --------------------------------------------------------------------------- #
# Authoritative sink: routes decoded canonical events into a real TickEngine
# --------------------------------------------------------------------------- #
class _EngineSink:
    """A ``ShadowMarketEventSink``-shaped sink that drives an authoritative ``TickEngine``.

    Records the raw delivery count so a duplicate that reaches the sink is provable (the shadow
    diagnostic view), while the engine's own gates decide whether the authoritative state mutates.
    """

    def __init__(self, engine: TickEngine) -> None:
        self._engine = engine
        self.raw_delivered_total = 0

    async def apply(self, envelope: MarketEventEnvelope, event: IpcPayload) -> None:
        self.raw_delivered_total += 1
        if isinstance(event, FeedContinuityEvent):
            self._engine.on_feed_continuity(event)
            return
        self._engine.process(event)


def _engine() -> TickEngine:
    registry = InstrumentStateRegistry(_instrument(symbol) for symbol in _SYMBOLS)
    return TickEngine(registry=registry, bus=EventBus(), clock=ManualClock(_CLOCK))


def _state(engine: TickEngine) -> dict[str, tuple[object, ...]]:
    """The authoritative per-instrument snapshot that must be identical to a single application."""
    snapshot: dict[str, tuple[object, ...]] = {}
    for symbol in _SYMBOLS:
        state = engine._registry.get(_instrument(symbol))
        if state is None or state.context is None:
            continue
        context = state.context
        snapshot[symbol] = (
            context.version,
            context.latest_tick,
            context.latest_quote,
            context.previous_close,
        )
    return snapshot


# --------------------------------------------------------------------------- #
# Consumer wiring + crash doubles (mirror the H4B/H4C/H4D integration helpers)
# --------------------------------------------------------------------------- #
def _config(**overrides: object) -> MarketIpcConfig:
    return MarketIpcConfig(block_ms=0, **overrides)


def _fast_idle(config: MarketIpcConfig, *, consumer_name: str) -> MarketIpcConfig:
    return config.model_copy(update={"consumer_name": consumer_name, "claim_idle_ms": 1})


def _durable(redis: Redis, config: MarketIpcConfig) -> CompositeDeduplicator:
    return CompositeDeduplicator(
        memory=BoundedDeduplicator(config.dedup_max_entries),
        durable=DurableDeduplicator(redis, config),
    )


def _consumer(
    redis: Redis,
    config: MarketIpcConfig,
    sink: _EngineSink,
    *,
    transport: object | None = None,
    deduplicator: object | None = None,
) -> MarketEventConsumer:
    return MarketEventConsumer(
        transport=transport or RedisMarketEventStream(redis=redis, config=config),  # type: ignore[arg-type]
        config=config,
        sink=sink,  # type: ignore[arg-type]
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: 7,
        now=lambda: _CLOCK,
        deduplicator=deduplicator or _durable(redis, config),  # type: ignore[arg-type]
    )


async def _pending(redis: Redis, config: MarketIpcConfig) -> int:
    summary = await redis.xpending(config.stream_name, config.consumer_group)
    return int(summary["pending"])


async def _drain(consumer: MarketEventConsumer, redis: Redis, config: MarketIpcConfig) -> None:
    for _ in range(400):
        before = consumer.diagnostics().acked_total
        await consumer.poll_once()
        if consumer.diagnostics().acked_total == before and await _pending(redis, config) == 0:
            return
    raise AssertionError("stream did not drain within the cycle budget")


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


class _RecordCrashDedup:
    """Dedup whose durable ``record`` raises (the apply->mark crash); ``contains`` stays real."""

    def __init__(self, delegate: CompositeDeduplicator) -> None:
        self._delegate = delegate

    async def contains(self, identity: ProducerEventIdentity) -> bool:
        return await self._delegate.contains(identity)

    async def record(self, identity: ProducerEventIdentity) -> None:
        raise RedisError("simulated crash before durable mark")


class _MarkCrashOnceEveryN:
    """Crash the durable ``record`` the FIRST time it is seen for every Nth sequence, then heal.

    Forces ~1/N of the replay through the real apply->mark crash window: the engine applies, the
    mark is lost, the entry stays pending, and a later reclaim re-delivers it so the engine's gates
    must absorb the duplicate. The retry (after reclaim) records normally so the stream drains.
    """

    def __init__(self, delegate: CompositeDeduplicator, *, every: int) -> None:
        self._delegate = delegate
        self._every = every
        self._crashed: set[ProducerEventIdentity] = set()

    async def contains(self, identity: ProducerEventIdentity) -> bool:
        return await self._delegate.contains(identity)

    async def record(self, identity: ProducerEventIdentity) -> None:
        if identity.producer_sequence % self._every == 0 and identity not in self._crashed:
            self._crashed.add(identity)
            raise RedisError("simulated crash before durable mark")
        await self._delegate.record(identity)


async def _publish(
    redis: Redis, config: MarketIpcConfig, envelopes: list[MarketEventEnvelope]
) -> None:
    producer = RedisMarketEventStream(redis=redis, config=config)
    await producer.ensure_group()
    for envelope in envelopes:
        await producer.publish(envelope)


# =========================================================================== #
# Baseline (C0/C1/C5): healthy replay applies every identity exactly once
# =========================================================================== #
async def test_h8a_healthy_replay_applies_each_identity_once(redis: Redis) -> None:
    config = _config(read_count=500)
    envelopes = _distinct_fixture(300)
    await _publish(redis, config, envelopes)
    await _publish(redis, config, envelopes)  # republish all: C1 must suppress before the engine

    engine = _engine()
    sink = _EngineSink(engine)
    consumer = _consumer(redis, _fast_idle(config, consumer_name="c"), sink)
    await consumer.start()
    await _drain(consumer, redis, config)

    # Each identity applied to the engine exactly once (C1 suppressed the 300 republished dups).
    assert sink.raw_delivered_total == 300
    assert consumer.diagnostics().duplicate_total == 300
    # Every distinct event advanced exactly one instrument's context by one version, so the
    # authoritative version total equals the distinct-event count (no dup minted a version).
    total_versions = sum(v[0] for v in _state(engine).values())
    assert total_versions == 300
    assert await _pending(redis, config) == 0


# =========================================================================== #
# T04/T05 (C3): apply->mark crash on a TICK -> reclaim re-applies -> no second mutation
# =========================================================================== #
async def test_h8a_t04_t05_apply_mark_crash_tick_reclaim_no_second_mutation(redis: Redis) -> None:
    config = _config()
    control = _envelope(_tick("TCS", seq=1, price="100"), seq=1)
    b2 = _envelope(_tick("TCS", seq=2, price="200"), seq=2)
    await _publish(redis, config, [control])

    engine = _engine()
    sink = _EngineSink(engine)
    normal = _consumer(redis, config, sink)
    await normal.start()
    await normal.poll_once()  # control: applied, marked, ACKed
    assert engine._registry.get(_instrument("TCS")).context.version == 1

    await _publish(redis, config, [b2])
    a_config = config.model_copy(update={"consumer_name": "A"})
    a = _consumer(redis, a_config, sink, deduplicator=_RecordCrashDedup(_durable(redis, a_config)))
    await a.start()
    await a.poll_once()  # b2: engine applied (v2), durable mark crashes -> pending, NOT marked
    assert sink.raw_delivered_total == 2
    assert engine._registry.get(_instrument("TCS")).context.version == 2
    assert await _pending(redis, config) == 1

    b = _consumer(redis, _fast_idle(config, consumer_name="B"), sink)
    await b.start()
    await asyncio.sleep(0.02)
    await b.poll_once()  # reclaim -> contains False -> re-deliver -> engine DUPLICATE gate holds
    assert sink.raw_delivered_total == 3  # the duplicate genuinely reached the sink (non-tautology)
    context = engine._registry.get(_instrument("TCS")).context
    assert context.version == 2  # the re-apply minted NO new version: authoritative state intact
    assert context.latest_tick is not None and context.latest_tick.last_price == Decimal("200")
    assert await _pending(redis, config) == 0


# =========================================================================== #
# T16 (C3): apply->mark crash on a MarketReference -> the reference gate holds across reclaim
# =========================================================================== #
async def test_h8a_t16_apply_mark_crash_reference_reclaim_reference_gate_holds(
    redis: Redis,
) -> None:
    config = _config()
    seed = _envelope(_tick("INFY", seq=1, price="100"), seq=1)
    b2_ref = _envelope(_reference("INFY", previous_close="123.45"), seq=2)
    await _publish(redis, config, [seed])

    engine = _engine()
    sink = _EngineSink(engine)
    normal = _consumer(redis, config, sink)
    await normal.start()
    await normal.poll_once()  # seed tick applied (v1)

    await _publish(redis, config, [b2_ref])
    a_config = config.model_copy(update={"consumer_name": "A"})
    a = _consumer(redis, a_config, sink, deduplicator=_RecordCrashDedup(_durable(redis, a_config)))
    await a.start()
    await a.poll_once()  # reference applied (v2, previous_close set), mark crashes -> pending
    assert engine._registry.get(_instrument("INFY")).context.previous_close == Decimal("123.45")
    assert engine._registry.get(_instrument("INFY")).context.version == 2

    b = _consumer(redis, _fast_idle(config, consumer_name="B"), sink)
    await b.start()
    await asyncio.sleep(0.02)
    await b.poll_once()  # reclaim re-delivers the reference: the reference gate suppresses it
    assert sink.raw_delivered_total == 3  # reference re-reached the sink
    context = engine._registry.get(_instrument("INFY")).context
    assert context.version == 2  # NO extra version bump from the duplicate reference
    assert context.previous_close == Decimal("123.45")
    assert await _pending(redis, config) == 0


# =========================================================================== #
# T06 (C4): ACK lost after a successful mark -> C1 suppresses reclaim -> engine not re-invoked
# =========================================================================== #
async def test_h8a_t06_ack_lost_after_mark_engine_not_reapplied(redis: Redis) -> None:
    config = _config()
    envelopes = [_envelope(_tick("RELIANCE", seq=i, price=str(100 + i)), seq=i) for i in (1, 2, 3)]
    await _publish(redis, config, envelopes)

    engine = _engine()
    sink = _EngineSink(engine)
    a_config = config.model_copy(update={"consumer_name": "A"})
    a = _consumer(
        redis,
        a_config,
        sink,
        transport=_AckCrashTransport(RedisMarketEventStream(redis=redis, config=a_config)),
    )
    await a.start()
    await a.poll_once()  # all 3 applied + durably marked, ACK fails -> all pending
    assert sink.raw_delivered_total == 3
    assert engine._registry.get(_instrument("RELIANCE")).context.version == 3
    assert await _pending(redis, config) == 3

    b = _consumer(redis, _fast_idle(config, consumer_name="B"), sink)
    await b.start()
    await asyncio.sleep(0.02)
    await _drain(b, redis, config)  # reclaim: C1 contains True -> DUPLICATE -> sink NOT called
    assert sink.raw_delivered_total == 3  # engine never re-invoked despite the ACK loss
    assert engine._registry.get(_instrument("RELIANCE")).context.version == 3
    assert await _pending(redis, config) == 0


# =========================================================================== #
# T33/T34: duplicate-stress replay through the real boundary matches the clean baseline exactly
# =========================================================================== #
async def test_h8a_t33_duplicate_stress_matches_clean_baseline(redis: Redis) -> None:
    count = 2_000
    envelopes = _distinct_fixture(count)

    clean_config = _config(read_count=1_000)
    await _publish(redis, clean_config, envelopes)
    clean_engine = _engine()
    clean = _consumer(
        redis, _fast_idle(clean_config, consumer_name="clean"), _EngineSink(clean_engine)
    )
    await clean.start()
    await _drain(clean, redis, clean_config)
    baseline = _state(clean_engine)

    await redis.flushall()
    dirty_config = _config(read_count=1_000)
    await _publish(redis, dirty_config, envelopes)
    dirty_engine = _engine()
    sink = _EngineSink(dirty_engine)
    # ~1/7 of identities crash their mark once, forcing them through the apply->mark window.
    dedup = _MarkCrashOnceEveryN(_durable(redis, dirty_config), every=7)
    dirty = _consumer(
        redis, _fast_idle(dirty_config, consumer_name="dirty"), sink, deduplicator=dedup
    )
    await dirty.start()
    await asyncio.sleep(0.02)
    await _drain(dirty, redis, dirty_config)

    assert sink.raw_delivered_total > count  # duplicates genuinely reached the sink (non-tautology)
    assert _state(dirty_engine) == baseline  # authoritative state identical to a single application
    assert await _pending(redis, dirty_config) == 0
