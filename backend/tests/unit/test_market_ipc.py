"""IPC contracts: envelope, serialization, identity/dedup, versioning, state, transport (PHASE A).

Covers the Phase-A test matrix: round-trips for every supported kind, producer epoch/sequence
semantics, dedup identity rules, fail-closed validation, timezone + Decimal fidelity, additive
compatibility, bounded payloads, config bounds, compacted-reference round-trip + trading-date
isolation, previous_close/session_open preservation-without-fabrication, health serialization,
priority classification, transport-disabled-by-default, no-secret-leakage, and a serialization
benchmark with representative payload sizes.
"""

from __future__ import annotations

import json
import statistics
import time
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.market_ipc import (
    BoundedDeduplicator,
    CompactedReferenceState,
    EventKind,
    EventPriority,
    IngestionHealthState,
    InMemoryCompactedReferenceStore,
    InMemoryMarketEventStream,
    MarketEventEnvelope,
    MarketIpcConfig,
    ProducerEventIdentity,
    RedisMarketEventStream,
    UniverseVersionComparison,
    build_envelope,
    compare_universe_version,
    decode_envelope,
    decode_payload,
    encode_envelope,
    identity_string_for,
    is_stale_trading_date,
    priority_for,
    reference_key,
)
from app.schemas.market_data import (
    FeedContinuity,
    FeedContinuityEvent,
    Instrument,
    MarketReference,
    ProviderSessionOhlc,
    ProviderStatus,
    Quote,
    Tick,
)

_IST = timezone(timedelta(hours=5, minutes=30))
_TS = datetime(2026, 9, 9, 10, 15, 30, tzinfo=_IST)
_TD = date(2026, 9, 9)


def _instrument() -> Instrument:
    return Instrument(exchange="NSE", symbol="TCS")


def _tick() -> Tick:
    return Tick(
        instrument=_instrument(),
        event_timestamp=_TS,
        last_price=Decimal("3456.75"),
        traded_quantity=5,
        session_cumulative_volume=120_000,
        session_ohlc=ProviderSessionOhlc(
            open_price=Decimal("3400.10"),
            high_price=Decimal("3499.90"),
            low_price=Decimal("3390.05"),
            close_price=Decimal("3456.75"),
        ),
    )


def _quote() -> Quote:
    return Quote(
        instrument=_instrument(),
        event_timestamp=_TS,
        bid_price=Decimal("3456.50"),
        ask_price=Decimal("3456.90"),
        bid_quantity=300,
        ask_quantity=150,
    )


def _reference() -> MarketReference:
    return MarketReference(instrument=_instrument(), previous_close=Decimal("3410.25"))


def _continuity() -> FeedContinuityEvent:
    return FeedContinuityEvent(status=FeedContinuity.CONNECTED, observed_at=_TS)


def _envelope(payload, *, epoch: int = 1, sequence: int = 0) -> MarketEventEnvelope:
    return build_envelope(
        payload,
        producer_id="ingestion-1",
        producer_epoch=epoch,
        producer_sequence=sequence,
        produced_at=_TS,
        trading_date=_TD,
        universe_version=7,
    )


def _roundtrip(payload):
    return decode_payload(*_decoded_kind_payload(payload))


def _decoded_kind_payload(payload):
    env = decode_envelope(encode_envelope(_envelope(payload)))
    return env.event_kind, env.payload


# --------------------------------------------------------------------------- #
# 1-4  payload round-trips
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("factory", [_tick, _quote, _reference, _continuity])
def test_payload_round_trip(factory) -> None:
    payload = factory()
    assert _roundtrip(payload) == payload


def test_envelope_kind_matches_payload() -> None:
    assert decode_envelope(encode_envelope(_envelope(_tick()))).event_kind == EventKind.TICK
    assert decode_envelope(encode_envelope(_envelope(_reference()))).event_kind == (
        EventKind.MARKET_REFERENCE
    )


# --------------------------------------------------------------------------- #
# 5-6  producer epoch / sequence
# --------------------------------------------------------------------------- #
def test_producer_epoch_and_sequence_round_trip() -> None:
    env = decode_envelope(encode_envelope(_envelope(_tick(), epoch=42, sequence=9999)))
    assert (env.producer_epoch, env.producer_sequence) == (42, 9999)


def test_producer_sequence_zero_ok_negative_rejected() -> None:
    assert _envelope(_tick(), sequence=0).producer_sequence == 0
    with pytest.raises(ValidationError):
        _envelope(_tick(), sequence=-1)
    with pytest.raises(ValidationError):
        _envelope(_tick(), epoch=-1)


# --------------------------------------------------------------------------- #
# 7-9  dedup identity
# --------------------------------------------------------------------------- #
def test_dedup_detects_exact_replay_only() -> None:
    dedup = BoundedDeduplicator(max_entries=1000)
    base = ProducerEventIdentity("p", 1, 5)
    assert dedup.is_duplicate(base) is False
    assert dedup.is_duplicate(base) is True  # exact replay


def test_same_sequence_different_epoch_not_duplicate() -> None:
    dedup = BoundedDeduplicator(max_entries=1000)
    assert dedup.is_duplicate(ProducerEventIdentity("p", 1, 5)) is False
    assert dedup.is_duplicate(ProducerEventIdentity("p", 2, 5)) is False  # producer restart


def test_same_epoch_different_producer_not_duplicate() -> None:
    dedup = BoundedDeduplicator(max_entries=1000)
    assert dedup.is_duplicate(ProducerEventIdentity("p", 1, 5)) is False
    assert dedup.is_duplicate(ProducerEventIdentity("q", 1, 5)) is False


def test_dedup_is_bounded_and_evicts_oldest() -> None:
    dedup = BoundedDeduplicator(max_entries=2)
    dedup.is_duplicate(ProducerEventIdentity("p", 1, 0))
    dedup.is_duplicate(ProducerEventIdentity("p", 1, 1))
    dedup.is_duplicate(ProducerEventIdentity("p", 1, 2))  # evicts sequence 0
    assert len(dedup) == 2
    assert dedup.is_duplicate(ProducerEventIdentity("p", 1, 0)) is False  # re-seen as new


# --------------------------------------------------------------------------- #
# 10-16  fail-closed validation
# --------------------------------------------------------------------------- #
def _mutate(payload, **changes) -> dict:
    raw = json.loads(encode_envelope(_envelope(payload)))
    raw.update(changes)
    return raw


def test_malformed_schema_version_type_rejected() -> None:
    with pytest.raises(ValidationError):
        decode_envelope(json.dumps(_mutate(_tick(), schema_version="abc")))


def test_unsupported_schema_version_rejected() -> None:
    with pytest.raises(ValidationError):
        decode_envelope(json.dumps(_mutate(_tick(), schema_version=2)))


def test_missing_required_field_rejected() -> None:
    raw = json.loads(encode_envelope(_envelope(_tick())))
    del raw["producer_epoch"]
    with pytest.raises(ValidationError):
        decode_envelope(json.dumps(raw))


def test_malformed_event_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        decode_envelope(json.dumps(_mutate(_tick(), event_kind="bogus")))


def test_malformed_trading_date_rejected() -> None:
    with pytest.raises(ValidationError):
        decode_envelope(json.dumps(_mutate(_tick(), trading_date="not-a-date")))


def test_malformed_universe_version_rejected() -> None:
    with pytest.raises(ValidationError):
        decode_envelope(json.dumps(_mutate(_tick(), universe_version=-1)))


def test_malformed_instrument_identity_rejected() -> None:
    with pytest.raises(ValidationError):
        decode_envelope(json.dumps(_mutate(_tick(), instrument_identity="TCS")))
    with pytest.raises(ValidationError):
        decode_envelope(json.dumps(_mutate(_tick(), instrument_identity="NSE:")))


def test_feed_wide_identity_for_continuity() -> None:
    assert identity_string_for(_continuity()) == "*:*"
    assert decode_envelope(encode_envelope(_envelope(_continuity()))).instrument_identity == "*:*"


# --------------------------------------------------------------------------- #
# 17-18  timezone + numeric fidelity
# --------------------------------------------------------------------------- #
def test_timezone_normalized_and_instant_preserved() -> None:
    env = decode_envelope(encode_envelope(_envelope(_tick())))
    assert env.produced_at == _TS.astimezone(UTC)
    assert env.produced_at.tzinfo == UTC
    tick = decode_payload(env.event_kind, env.payload)
    assert tick.event_timestamp == _TS.astimezone(UTC)


def test_decimal_precision_preserved() -> None:
    ref = MarketReference(instrument=_instrument(), previous_close=Decimal("1234.55"))
    restored = _roundtrip(ref)
    assert restored.previous_close == Decimal("1234.55")
    assert str(restored.previous_close) == "1234.55"


# --------------------------------------------------------------------------- #
# 19-20  compatibility + bounded payload
# --------------------------------------------------------------------------- #
def test_unknown_additive_field_tolerated() -> None:
    raw = _mutate(_tick(), some_future_additive_field={"nested": 1})
    env = decode_envelope(json.dumps(raw))
    assert env.event_kind == EventKind.TICK
    assert not hasattr(env, "some_future_additive_field")


def test_encode_rejects_oversize_payload() -> None:
    env = _envelope(_tick())
    with pytest.raises(ValueError, match="max_bytes"):
        encode_envelope(env, max_bytes=10)


def test_envelope_field_rejects_payload_over_hard_ceiling() -> None:
    with pytest.raises(ValidationError):
        MarketEventEnvelope(
            schema_version=1,
            producer_id="p",
            producer_epoch=0,
            producer_sequence=0,
            produced_at=_TS,
            event_kind=EventKind.TICK,
            trading_date=_TD,
            universe_version=0,
            instrument_identity="NSE:TCS",
            payload="x" * 300_000,
        )


# --------------------------------------------------------------------------- #
# 21  config bounds
# --------------------------------------------------------------------------- #
def test_transport_disabled_by_default() -> None:
    assert MarketIpcConfig().enabled is False


def test_config_defaults_and_bounds() -> None:
    config = MarketIpcConfig()
    assert config.stream_name == "md:events"
    assert config.consumer_group == "backend"
    with pytest.raises(ValidationError):
        MarketIpcConfig(maxlen=10)  # below floor
    with pytest.raises(ValidationError):
        MarketIpcConfig(read_count=0)
    with pytest.raises(ValidationError):
        MarketIpcConfig(stream_name="has space")


# --------------------------------------------------------------------------- #
# 22-25  compacted reference state
# --------------------------------------------------------------------------- #
def _ref_state(td: date = _TD, **fields) -> CompactedReferenceState:
    return CompactedReferenceState(
        instrument_identity="NSE:TCS", trading_date=td, updated_at=_TS, **fields
    )


async def test_compacted_reference_round_trip() -> None:
    store = InMemoryCompactedReferenceStore()
    state = _ref_state(previous_close=Decimal("3410.25"), session_open=Decimal("3400.10"))
    await store.put(state)
    assert await store.get(_TD, "NSE:TCS") == state
    assert await store.all(_TD) == (state,)


async def test_compacted_reference_trading_date_isolation() -> None:
    store = InMemoryCompactedReferenceStore()
    await store.put(_ref_state(td=date(2026, 9, 8), previous_close=Decimal("100")))
    await store.put(_ref_state(td=date(2026, 9, 9), previous_close=Decimal("200")))
    latest = await store.get(date(2026, 9, 9), "NSE:TCS")
    assert latest is not None and latest.previous_close == Decimal("200")
    assert await store.all(date(2026, 9, 8)) != await store.all(date(2026, 9, 9))


def test_previous_close_preserved_via_json() -> None:
    state = _ref_state(previous_close=Decimal("3410.25"))
    restored = CompactedReferenceState.model_validate_json(state.model_dump_json())
    assert restored.previous_close == Decimal("3410.25")


def test_session_open_preserved_and_never_fabricated() -> None:
    absent = _ref_state()
    assert absent.session_open is None  # not fabricated when unknown
    present = _ref_state(session_open=Decimal("3400.10"))
    restored = CompactedReferenceState.model_validate_json(present.model_dump_json())
    assert restored.session_open == Decimal("3400.10")


# --------------------------------------------------------------------------- #
# 26  health serialization
# --------------------------------------------------------------------------- #
def test_health_state_serialization_round_trip() -> None:
    health = IngestionHealthState(
        producer_id="ingestion-1",
        producer_epoch=3,
        updated_at=_TS,
        ingestion=ProviderStatus.HEALTHY,
        transport=ProviderStatus.DEGRADED,
        universe_sync=ProviderStatus.HEALTHY,
        market_data_age_seconds=1.5,
        last_event_at=_TS,
    )
    restored = IngestionHealthState.model_validate_json(health.model_dump_json())
    assert restored == health
    assert restored.transport is ProviderStatus.DEGRADED


# --------------------------------------------------------------------------- #
# 27  priority + version/date helpers
# --------------------------------------------------------------------------- #
def test_event_priority_classification() -> None:
    assert priority_for(EventKind.MARKET_REFERENCE) is EventPriority.CORRECTNESS_CRITICAL
    assert priority_for(EventKind.FEED_CONTINUITY) is EventPriority.CORRECTNESS_CRITICAL
    assert priority_for(EventKind.TICK) is EventPriority.HIGH_FREQUENCY
    assert priority_for(EventKind.QUOTE) is EventPriority.HIGH_FREQUENCY


def test_universe_version_comparison() -> None:
    assert compare_universe_version(7, 7) is UniverseVersionComparison.MATCH
    assert compare_universe_version(6, 7) is UniverseVersionComparison.OLDER
    assert compare_universe_version(8, 7) is UniverseVersionComparison.NEWER
    assert compare_universe_version(7, None) is UniverseVersionComparison.UNKNOWN


def test_stale_trading_date_predicate() -> None:
    assert is_stale_trading_date(date(2026, 9, 8), date(2026, 9, 9)) is True
    assert is_stale_trading_date(date(2026, 9, 9), date(2026, 9, 9)) is False
    assert is_stale_trading_date(date(2026, 9, 9), None) is False


def test_reference_key_naming() -> None:
    assert reference_key("md:reference", _TD) == "md:reference:2026-09-09"


# --------------------------------------------------------------------------- #
# 28  no-secret-leakage (security)
# --------------------------------------------------------------------------- #
def test_no_credentials_or_provider_wire_concepts_in_envelope() -> None:
    wire = encode_envelope(_envelope(_tick())).decode()
    lowered = wire.lower()
    for forbidden in ("token", "totp", "password", "authorization", "client_id", "security_id"):
        assert forbidden not in lowered
    parsed = set(json.loads(wire))
    assert parsed == {
        "schema_version",
        "producer_id",
        "producer_epoch",
        "producer_sequence",
        "produced_at",
        "event_kind",
        "trading_date",
        "universe_version",
        "instrument_identity",
        "payload",
    }


# --------------------------------------------------------------------------- #
# transport contract (in-memory reference)
# --------------------------------------------------------------------------- #
async def test_in_memory_stream_publish_read_ack_claim() -> None:
    stream = InMemoryMarketEventStream()
    await stream.ensure_group()
    await stream.publish(_envelope(_tick(), sequence=0))
    await stream.publish(_envelope(_quote(), sequence=1))
    batch = await stream.read()
    assert [env.event_kind for _mid, env in batch] == [EventKind.TICK, EventKind.QUOTE]
    assert len(await stream.claim_stale()) == 2  # unacked
    acked = await stream.ack(*[mid for mid, _env in batch])
    assert acked == 2
    assert await stream.claim_stale() == []
    assert await stream.read() == []  # cursor advanced


def test_redis_stream_uses_configured_names() -> None:
    config = MarketIpcConfig(stream_name="md:events", consumer_group="backend")
    stream = RedisMarketEventStream(
        redis=object(), config=config
    )  # not connected; construction only
    assert stream._config.stream_name == "md:events"


# --------------------------------------------------------------------------- #
# serialization benchmark + representative sizes (reported, not optimized)
# --------------------------------------------------------------------------- #
def test_serialization_benchmark_and_sizes(capsys) -> None:
    samples = {"tick": _tick(), "quote": _quote(), "reference": _reference()}
    sizes = {name: len(encode_envelope(_envelope(p))) for name, p in samples.items()}
    durations: list[float] = []
    tick_env = _envelope(_tick())
    for _ in range(2000):
        start = time.perf_counter()
        raw = encode_envelope(tick_env)
        env = decode_envelope(raw)
        decode_payload(env.event_kind, env.payload)
        durations.append((time.perf_counter() - start) * 1_000_000)  # microseconds
    durations.sort()
    stats = {
        "median_us": round(statistics.median(durations), 2),
        "p95_us": round(durations[int(len(durations) * 0.95)], 2),
        "max_us": round(durations[-1], 2),
    }
    with capsys.disabled():
        print(f"\nIPC round-trip (tick) us: {stats}")
        print(f"IPC payload sizes (bytes): {sizes}")
    assert stats["p95_us"] < 500  # generous regression ceiling, not a production claim
    assert all(size < 2000 for size in sizes.values())
