"""Real disposable-Redis integration for atomic stream+reference publication (DECOUPLING D1).

Runs a real ``redis-server`` (bundled by ``redislite`` on a private unix socket). Proves the D1
invariant with a real Redis + real Lua: a STREAM_PLUS_REFERENCE publication commits the canonical
stream append AND the compacted-reference transition together (or, on pre-write validation
failure, neither); a STREAM_ONLY publication never touches a reference key; the reference
transition keeps Phase-D monotonic/non-destructive semantics and TTL; concurrent writers
converge on the highest ordering. Skips if redislite is absent.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.market_ipc import (
    MarketEventEnvelope,
    MarketIpcConfig,
    RedisAtomicPublisher,
    ReferenceOutcome,
    build_envelope,
    reference_from_envelope,
    reference_key,
)
from app.market_ipc.atomic import _PUBLISH_STREAM_AND_REFERENCE_LUA
from app.schemas.market_data import (
    Instrument,
    MarketReference,
    ProviderSessionOhlc,
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


def _instrument(symbol: str = "TCS") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _ref_env(
    previous_close: str, *, epoch: int = 1, seq: int, symbol: str = "TCS"
) -> MarketEventEnvelope:
    return build_envelope(
        MarketReference(instrument=_instrument(symbol), previous_close=Decimal(previous_close)),
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=7,
    )


def _tick_env(
    open_price: str, *, epoch: int = 1, seq: int, symbol: str = "TCS"
) -> MarketEventEnvelope:
    return build_envelope(
        Tick(
            instrument=_instrument(symbol),
            event_timestamp=_NOW,
            last_price=Decimal("100"),
            session_ohlc=ProviderSessionOhlc(
                open_price=Decimal(open_price),
                high_price=Decimal("3450"),
                low_price=Decimal("3390"),
                close_price=Decimal("3420"),
            ),
        ),
        producer_id=_PRODUCER,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=7,
    )


def _quote_env(*, seq: int) -> MarketEventEnvelope:
    return build_envelope(
        Quote(
            instrument=_instrument(),
            event_timestamp=_NOW,
            bid_price=Decimal("101"),
            ask_price=Decimal("102"),
            bid_quantity=1,
            ask_quantity=1,
        ),
        producer_id=_PRODUCER,
        producer_epoch=1,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=7,
    )


def _publisher(redis: Redis) -> RedisAtomicPublisher:
    return RedisAtomicPublisher(redis=redis, config=MarketIpcConfig())


def _ref_key() -> str:
    return reference_key(MarketIpcConfig().reference_key_prefix, _TD)


async def _stored(redis: Redis) -> dict[str, object] | None:
    raw = await redis.hget(_ref_key(), "NSE:TCS")
    if raw is None:
        return None
    import json

    return json.loads(raw)


# --- A / P: STREAM_ONLY never touches the reference key ---------------------- #
async def test_stream_only_appends_stream_and_leaves_reference_absent(redis: Redis) -> None:
    result = await _publisher(redis).publish_stream_only(_quote_env(seq=1))
    assert result.reference_outcome is ReferenceOutcome.NO_REFERENCE_DATA
    assert await redis.xlen(MarketIpcConfig().stream_name) == 1
    assert await redis.exists(_ref_key()) == 0  # no reference key, no TTL


# --- B: STREAM_PLUS_REFERENCE commits BOTH ---------------------------------- #
async def test_stream_plus_reference_commits_both(redis: Redis) -> None:
    result = await _publisher(redis).publish_stream_and_reference(
        _ref_env("100.5", seq=1), reference_from_envelope(_ref_env("100.5", seq=1))
    )
    assert result.reference_outcome is ReferenceOutcome.WRITTEN
    assert await redis.xlen(MarketIpcConfig().stream_name) == 1  # stream committed
    stored = await _stored(redis)
    assert stored is not None and stored["previous_close"] == "100.5"  # reference committed


# --- §25 fault injection: invalid args -> NEITHER commits ------------------- #
async def test_invalid_args_commit_neither_half(redis: Redis) -> None:
    # Drive the raw Lua with a non-numeric epoch: validation happens before the first write,
    # so the error must leave BOTH the stream and the reference untouched (no partial window).
    script = redis.register_script(_PUBLISH_STREAM_AND_REFERENCE_LUA)
    env = _ref_env("100.5", seq=1)
    with pytest.raises(ResponseError, match="D1_INVALID_ARGS"):
        await script(
            keys=[MarketIpcConfig().stream_name, _ref_key()],
            args=[
                "e",
                b"x",
                MarketIpcConfig().maxlen,
                "NSE:TCS",
                "not-a-number",  # malformed epoch
                1,
                reference_from_envelope(env).model_dump_json(),
                MarketIpcConfig().reference_ttl_seconds,
            ],
        )
    assert await redis.xlen(MarketIpcConfig().stream_name) == 0  # stream NOT appended
    assert await redis.exists(_ref_key()) == 0  # reference NOT written


# --- E / H: newer position -> reference progresses, stream grows ------------- #
async def test_newer_reference_progresses(redis: Redis) -> None:
    pub = _publisher(redis)
    await pub.publish_stream_and_reference(
        _ref_env("100", seq=1), reference_from_envelope(_ref_env("100", seq=1))
    )
    r2 = await pub.publish_stream_and_reference(
        _ref_env("101", seq=2), reference_from_envelope(_ref_env("101", seq=2))
    )
    assert r2.reference_outcome is ReferenceOutcome.MERGED
    assert (await _stored(redis))["previous_close"] == "101"
    assert await redis.xlen(MarketIpcConfig().stream_name) == 2  # both canonical events appended


# --- F: older position -> reference no-op BUT stream still appended (§10) ---- #
async def test_older_reference_is_noop_but_stream_still_appends(redis: Redis) -> None:
    pub = _publisher(redis)
    await pub.publish_stream_and_reference(
        _ref_env("101", seq=2), reference_from_envelope(_ref_env("101", seq=2))
    )
    older = _ref_env("100", seq=1)
    r = await pub.publish_stream_and_reference(older, reference_from_envelope(older))
    assert r.reference_outcome is ReferenceOutcome.STALE_REJECTED  # reference cannot regress
    assert (await _stored(redis))["previous_close"] == "101"  # unchanged
    assert await redis.xlen(MarketIpcConfig().stream_name) == 2  # older event still in history


# --- G: exact same position -> reference duplicate, stream still appends ----- #
async def test_same_position_reference_duplicate_stream_not_deduped(redis: Redis) -> None:
    pub = _publisher(redis)
    env = _ref_env("100", seq=1)
    await pub.publish_stream_and_reference(env, reference_from_envelope(env))
    r = await pub.publish_stream_and_reference(env, reference_from_envelope(env))
    assert r.reference_outcome is ReferenceOutcome.DUPLICATE
    assert await redis.xlen(MarketIpcConfig().stream_name) == 2  # D1 does not dedup the stream (C1)


# --- I / J: producer-epoch transition ordering (M1) ------------------------- #
async def test_higher_epoch_progresses_lower_epoch_rejected(redis: Redis) -> None:
    pub = _publisher(redis)
    # epoch 1, seq 5
    e1 = _ref_env("100", epoch=1, seq=5)
    await pub.publish_stream_and_reference(e1, reference_from_envelope(e1))
    # epoch 2, seq 1 -> newer incarnation, must progress even though seq resets
    e2 = _ref_env("102", epoch=2, seq=1)
    r2 = await pub.publish_stream_and_reference(e2, reference_from_envelope(e2))
    assert r2.reference_outcome is ReferenceOutcome.MERGED
    assert (await _stored(redis))["previous_close"] == "102"
    # a straggler from the old epoch must not regress the reference
    e1b = _ref_env("999", epoch=1, seq=9)
    r3 = await pub.publish_stream_and_reference(e1b, reference_from_envelope(e1b))
    assert r3.reference_outcome is ReferenceOutcome.STALE_REJECTED
    assert (await _stored(redis))["previous_close"] == "102"


# --- K / L / M / N: non-destructive merge + provenance + universe_version ---- #
async def test_merge_is_non_destructive_and_keeps_provenance(redis: Redis) -> None:
    pub = _publisher(redis)
    ref = _ref_env("100", seq=1)  # previous_close only
    await pub.publish_stream_and_reference(ref, reference_from_envelope(ref))
    tick = _tick_env("3400", seq=2)  # session OHLC only, no previous_close
    await pub.publish_stream_and_reference(tick, reference_from_envelope(tick))
    stored = await _stored(redis)
    assert stored["previous_close"] == "100"  # not erased by the later Tick
    assert stored["session_open"] == "3400"  # added by the Tick
    assert stored["producer_sequence"] == 2  # provenance = newer
    assert stored["universe_version"] == 7


# --- O: TTL applied on write; STREAM_ONLY sets no TTL ----------------------- #
async def test_ttl_applied_on_reference_write(redis: Redis) -> None:
    env = _ref_env("100", seq=1)
    await _publisher(redis).publish_stream_and_reference(env, reference_from_envelope(env))
    ttl = await redis.ttl(_ref_key())
    assert 0 < ttl <= MarketIpcConfig().reference_ttl_seconds


# --- Q: concurrent same-instrument converges on highest ordering ------------ #
async def test_concurrent_same_instrument_converges_on_highest(redis: Redis) -> None:
    pub = _publisher(redis)
    envs = [_ref_env(str(100 + i), seq=i) for i in range(1, 21)]
    await asyncio.gather(
        *(pub.publish_stream_and_reference(e, reference_from_envelope(e)) for e in envs)
    )
    assert (await _stored(redis))["previous_close"] == "120"  # highest seq 20 -> 100+20
    assert (await _stored(redis))["producer_sequence"] == 20
    assert await redis.xlen(MarketIpcConfig().stream_name) == 20  # every event in history


# --- R: concurrent different instruments stay independent ------------------- #
async def test_concurrent_different_instruments_independent(redis: Redis) -> None:
    pub = _publisher(redis)
    a = _ref_env("111", seq=1, symbol="TCS")
    b = _ref_env("222", seq=1, symbol="INFY")
    await asyncio.gather(
        pub.publish_stream_and_reference(a, reference_from_envelope(a)),
        pub.publish_stream_and_reference(b, reference_from_envelope(b)),
    )
    raw_a = await redis.hget(_ref_key(), "NSE:TCS")
    raw_b = await redis.hget(_ref_key(), "NSE:INFY")
    import json

    assert json.loads(raw_a)["previous_close"] == "111"
    assert json.loads(raw_b)["previous_close"] == "222"
    assert await redis.xlen(MarketIpcConfig().stream_name) == 2
