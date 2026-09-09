"""Real disposable-Redis integration for compacted reference recovery (DECOUPLING PHASE D).

Runs a real ``redis-server`` (bundled by ``redislite`` on a private unix socket) — never a
shared or production Redis. Verifies atomic monotonic compaction (WATCH/MULTI), non-destructive
merge, stale/duplicate rejection, TTL, trading-date isolation, malformed-entry isolation on load,
concurrent same-instrument updates, and backend-restart recovery scenarios. Skips if redislite is
absent.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.market_ipc import (
    MarketEventEnvelope,
    MarketIpcConfig,
    RedisCompactedReferenceStore,
    ReferenceOutcome,
    ReferenceStateLoader,
    ReferenceStateWriter,
    build_envelope,
    reference_from_envelope,
    reference_key,
)
from app.schemas.market_data import Instrument, MarketReference, ProviderSessionOhlc, Tick

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


def _instrument(symbol: str = "TCS") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _ohlc() -> ProviderSessionOhlc:
    return ProviderSessionOhlc(
        open_price=Decimal("3400"),
        high_price=Decimal("3450"),
        low_price=Decimal("3390"),
        close_price=Decimal("3420"),
    )


def _ref_env(
    previous_close: str, *, seq: int, symbol: str = "TCS", td: date = _TD
) -> MarketEventEnvelope:
    return build_envelope(
        MarketReference(instrument=_instrument(symbol), previous_close=Decimal(previous_close)),
        producer_id=_PRODUCER,
        producer_epoch=1,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=td,
        universe_version=7,
    )


def _tick_env(*, seq: int, symbol: str = "TCS", td: date = _TD) -> MarketEventEnvelope:
    return build_envelope(
        Tick(
            instrument=_instrument(symbol),
            event_timestamp=_NOW,
            last_price=Decimal("3410"),
            session_ohlc=_ohlc(),
        ),
        producer_id=_PRODUCER,
        producer_epoch=1,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=td,
        universe_version=7,
    )


def _store(redis: Redis, **overrides: object) -> RedisCompactedReferenceStore:
    return RedisCompactedReferenceStore(redis=redis, config=MarketIpcConfig(**overrides))


async def _compact(
    store: RedisCompactedReferenceStore, envelope: MarketEventEnvelope
) -> ReferenceOutcome:
    state = reference_from_envelope(envelope)
    assert state is not None
    return await store.compact(state)


# --------------------------------------------------------------------------- #
# write / read / merge / ordering
# --------------------------------------------------------------------------- #
async def test_compact_write_then_read_round_trip(redis: Redis) -> None:
    store = _store(redis)
    assert await _compact(store, _ref_env("3395.50", seq=1)) is ReferenceOutcome.WRITTEN
    state = await store.get(_TD, "NSE:TCS")
    assert state is not None and state.previous_close == Decimal("3395.50")


async def test_non_destructive_merge_over_real_redis(redis: Redis) -> None:
    store = _store(redis)
    await _compact(store, _ref_env("100", seq=1))  # previous_close
    assert await _compact(store, _tick_env(seq=2)) is ReferenceOutcome.MERGED  # session ohlc
    state = await store.get(_TD, "NSE:TCS")
    assert state is not None
    assert state.previous_close == Decimal("100")  # not cleared by the later tick
    assert state.session_open == Decimal("3400")


async def test_stale_and_duplicate_rejected(redis: Redis) -> None:
    store = _store(redis)
    await _compact(store, _ref_env("100", seq=5))
    assert await _compact(store, _ref_env("99", seq=4)) is ReferenceOutcome.STALE_REJECTED
    assert await _compact(store, _ref_env("100", seq=5)) is ReferenceOutcome.DUPLICATE
    state = await store.get(_TD, "NSE:TCS")
    assert state is not None and state.previous_close == Decimal("100")  # stale never overwrote


# --------------------------------------------------------------------------- #
# TTL + trading-date isolation
# --------------------------------------------------------------------------- #
async def test_ttl_is_bounded_and_set(redis: Redis) -> None:
    store = _store(redis, reference_ttl_seconds=3_600)
    await _compact(store, _ref_env("100", seq=1))
    ttl = await redis.ttl(reference_key("md:reference", _TD))
    assert 0 < ttl <= 3_600  # bounded retention set, today's key not deleted


async def test_trading_date_isolation_no_cross_day_leak(redis: Redis) -> None:
    store = _store(redis)
    await _compact(store, _ref_env("100", seq=1, td=date(2026, 9, 8)))
    await _compact(store, _ref_env("200", seq=1, td=date(2026, 9, 9)))
    loader = ReferenceStateLoader(source=store, now=lambda: _NOW)
    today = await loader.load(date(2026, 9, 9), expected_universe_version=7)
    assert today.states["NSE:TCS"].previous_close == Decimal("200")
    assert (await store.get(date(2026, 9, 8), "NSE:TCS")).previous_close == Decimal("100")


# --------------------------------------------------------------------------- #
# loader: malformed isolation + universe gate
# --------------------------------------------------------------------------- #
async def test_loader_isolates_malformed_entry(redis: Redis) -> None:
    store = _store(redis)
    await _compact(store, _ref_env("100", seq=1, symbol="TCS"))
    await redis.hset(reference_key("md:reference", _TD), "NSE:BAD", b"not-json")  # inject poison
    loader = ReferenceStateLoader(source=store, now=lambda: _NOW)
    snapshot = await loader.load(_TD, expected_universe_version=7)
    assert set(snapshot.states) == {"NSE:TCS"}
    assert snapshot.diagnostics.entries_invalid == 1
    assert snapshot.diagnostics.entries_loaded == 1


async def test_loader_universe_mismatch_excluded(redis: Redis) -> None:
    store = _store(redis)
    await _compact(store, _ref_env("100", seq=1))
    loader = ReferenceStateLoader(source=store, now=lambda: _NOW)
    snapshot = await loader.load(_TD, expected_universe_version=8)  # newer than stored 7
    assert snapshot.states == {}
    assert snapshot.diagnostics.universe_mismatch_count == 1


# --------------------------------------------------------------------------- #
# concurrency: no lost update, deterministic newest wins
# --------------------------------------------------------------------------- #
async def test_concurrent_updates_same_instrument_newest_wins(redis: Redis) -> None:
    store = _store(redis)
    # 20 concurrent compactions, each a distinct newer sequence with a distinct previous_close.
    envelopes = [_ref_env(str(1000 + i), seq=i) for i in range(1, 21)]
    await asyncio.gather(*(_compact(store, env) for env in envelopes))
    state = await store.get(_TD, "NSE:TCS")
    assert state is not None
    assert state.producer_sequence == 20  # highest ordering won; no lost update
    assert state.previous_close == Decimal("1020")


# --------------------------------------------------------------------------- #
# writer end-to-end + restart recovery
# --------------------------------------------------------------------------- #
async def test_writer_and_loader_recover_after_restart(redis: Redis) -> None:
    writer = ReferenceStateWriter(store=_store(redis), now=lambda: _NOW)
    await writer.update(_ref_env("3395.50", seq=1))  # previous_close (code-6)
    await writer.update(_tick_env(seq=2))  # session ohlc
    # "backend restart": a brand-new loader with no in-process state recovers from Redis alone.
    loader = ReferenceStateLoader(source=_store(redis), now=lambda: _NOW)
    snapshot = await loader.load(_TD, expected_universe_version=7)
    recovered = snapshot.states["NSE:TCS"]
    assert recovered.previous_close == Decimal("3395.50")
    assert recovered.session_open == Decimal("3400")
    assert snapshot.diagnostics.complete_reference_count == 1


async def test_backend_start_before_ingestion_is_warming_up(redis: Redis) -> None:
    loader = ReferenceStateLoader(source=_store(redis), now=lambda: _NOW)
    snapshot = await loader.load(_TD, expected_universe_version=7)
    assert snapshot.warming_up  # empty key -> no fabrication, no crash
    assert snapshot.diagnostics.entries_total == 0


async def test_loader_redis_down_raises_not_empty() -> None:
    broken = Redis(unix_socket_path="/nonexistent/apexscan-reference.sock")
    loader = ReferenceStateLoader(source=_store(broken), now=lambda: _NOW)
    with pytest.raises(RedisError):  # never silently reports an outage as "warming up"
        await loader.load(_TD, expected_universe_version=7)
    await broken.aclose()
