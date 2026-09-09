"""Unit tests for the shadow backend consumer (DECOUPLING PHASE C).

Drives :class:`MarketEventConsumer` against an in-memory raw transport fake so every branch of
the validation order, dedup commit timing, poison classification, ACK policy, and failure
isolation is exercised deterministically without Redis. Redis-primitive behaviour (XREADGROUP/
XPENDING/XACK/XAUTOCLAIM/>read_count recovery/restart) is covered by the integration suite.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from redis.exceptions import RedisError

from app.market_ipc import (
    SCHEMA_VERSION,
    BoundedDeduplicator,
    MarketEventConsumer,
    MarketEventEnvelope,
    MarketIpcConfig,
    RecordingShadowSink,
)
from app.market_ipc.envelope import identity_string_for
from app.market_ipc.events import EventKind, IpcPayload, encode_payload, event_kind_for
from app.market_ipc.transport import RawDeliveredEvent
from app.schemas.market_data import (
    FeedContinuity,
    FeedContinuityEvent,
    Instrument,
    MarketReference,
    Quote,
    Tick,
)

_NOW = datetime(2026, 9, 9, 10, 15, 30, tzinfo=UTC)
_TD = date(2026, 9, 9)
_PRODUCER = "market-ingestion"


def _instrument(symbol: str = "TCS") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _tick(symbol: str = "TCS", price: str = "100.5") -> Tick:
    return Tick(instrument=_instrument(symbol), event_timestamp=_NOW, last_price=Decimal(price))


def _quote() -> Quote:
    return Quote(
        instrument=_instrument(),
        event_timestamp=_NOW,
        bid_price=Decimal("101.10"),
        ask_price=Decimal("101.40"),
        bid_quantity=10,
        ask_quantity=20,
    )


def _envelope(
    payload: IpcPayload,
    *,
    kind: EventKind | None = None,
    seq: int,
    epoch: int = 1,
    producer: str = _PRODUCER,
    trading_date: date = _TD,
    universe_version: int = 7,
    payload_json: str | None = None,
) -> MarketEventEnvelope:
    return MarketEventEnvelope(
        schema_version=SCHEMA_VERSION,
        producer_id=producer,
        producer_epoch=epoch,
        producer_sequence=seq,
        produced_at=_NOW,
        event_kind=kind or event_kind_for(payload),
        trading_date=trading_date,
        universe_version=universe_version,
        instrument_identity=identity_string_for(payload),
        payload=payload_json if payload_json is not None else encode_payload(payload),
    )


def _raw(envelope: MarketEventEnvelope) -> bytes:
    return envelope.model_dump_json().encode("utf-8")


class FakeRawTransport:
    """In-memory raw stream: FIFO new queue + unacked pending set, with fault injection."""

    def __init__(self) -> None:
        self.new: list[RawDeliveredEvent] = []
        self.pending: dict[str, bytes | None] = {}
        self.acked: list[str] = []
        self.fail_read = False
        self.fail_ack = False
        self._counter = 0

    def push(self, raw: bytes | None) -> str:
        message_id = str(self._counter)
        self._counter += 1
        self.new.append((message_id, raw))
        return message_id

    async def ensure_group(self) -> None:
        return None

    async def read_raw(self) -> list[RawDeliveredEvent]:
        if self.fail_read:
            raise RedisError("read down")
        batch = self.new
        self.new = []
        for message_id, raw in batch:
            self.pending[message_id] = raw
        return batch

    async def claim_page_raw(self, start_id: str) -> tuple[str, list[RawDeliveredEvent]]:
        return "0-0", list(self.pending.items())

    async def ack(self, *message_ids: str) -> int:
        if self.fail_ack:
            raise RedisError("ack down")
        acked = 0
        for message_id in message_ids:
            if self.pending.pop(message_id, None) is not None:
                acked += 1
            self.acked.append(message_id)
        return acked


def _consumer(
    transport: FakeRawTransport,
    *,
    sink: RecordingShadowSink | None = None,
    current_date: date | None = _TD,
    current_universe: int | None = 7,
    now: datetime = _NOW,
    dedup: BoundedDeduplicator | None = None,
) -> MarketEventConsumer:
    return MarketEventConsumer(
        transport=transport,  # type: ignore[arg-type]  # test fake implements the used subset
        config=MarketIpcConfig(),
        sink=sink or RecordingShadowSink(),
        trading_date_source=lambda: current_date,
        universe_version_source=lambda: current_universe,
        now=lambda: now,
        deduplicator=dedup,
    )


# --------------------------------------------------------------------------- #
# A. Envelope handling + supported kinds
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload",
    [
        _tick(),
        _quote(),
        MarketReference(instrument=_instrument(), previous_close=Decimal("100.0")),
        FeedContinuityEvent(status=FeedContinuity.CONNECTED, observed_at=_NOW),
    ],
)
async def test_supported_kinds_apply_losslessly(payload: IpcPayload) -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    transport.push(_raw(_envelope(payload, seq=1)))
    consumer = _consumer(transport, sink=sink)
    await consumer.poll_once()
    assert sink.applied_total == 1
    assert sink.events[0][1] == payload  # canonical round-trip is lossless
    assert consumer.diagnostics().acked_total == 1
    assert transport.pending == {}


async def test_malformed_envelope_is_poison_acked_and_does_not_jam() -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    transport.push(b"this is not json")
    transport.push(_raw(_envelope(_tick(), seq=1)))
    consumer = _consumer(transport, sink=sink)
    await consumer.poll_once()
    diagnostics = consumer.diagnostics()
    assert diagnostics.envelope_decode_failures == 1
    assert sink.applied_total == 1  # the following valid event still processed
    assert transport.pending == {}  # poison terminally ACKed (not jamming the group)


async def test_missing_field_entry_is_poison_acked() -> None:
    transport = FakeRawTransport()
    transport.push(None)
    consumer = _consumer(transport)
    await consumer.poll_once()
    assert consumer.diagnostics().envelope_decode_failures == 1
    assert transport.pending == {}


async def test_unsupported_schema_fails_closed_and_never_reaches_sink() -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    raw = _raw(_envelope(_tick(), seq=1)).replace(b'"schema_version":1', b'"schema_version":999')
    transport.push(raw)
    consumer = _consumer(transport, sink=sink)
    await consumer.poll_once()
    assert consumer.diagnostics().unsupported_schema_total == 1
    assert sink.applied_total == 0
    assert transport.pending == {}


async def test_additive_unknown_envelope_field_is_tolerated() -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    raw = _raw(_envelope(_tick(), seq=1)).replace(b"{", b'{"future_field":"x",', 1)
    transport.push(raw)
    consumer = _consumer(transport, sink=sink)
    await consumer.poll_once()
    assert sink.applied_total == 1  # extra="ignore" boundary tolerance


async def test_event_kind_payload_mismatch_is_classified_and_not_applied() -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    # claim TICK but carry a Quote payload -> decodes under another kind -> mismatch
    envelope = _envelope(_quote(), kind=EventKind.TICK, seq=1)
    transport.push(_raw(envelope))
    consumer = _consumer(transport, sink=sink)
    await consumer.poll_once()
    assert consumer.diagnostics().event_kind_mismatch_total == 1
    assert sink.applied_total == 0
    assert transport.pending == {}


async def test_malformed_payload_is_classified_as_payload_decode_failure() -> None:
    transport = FakeRawTransport()
    envelope = _envelope(_tick(), seq=1, payload_json='{"garbage":1}')
    transport.push(_raw(envelope))
    consumer = _consumer(transport)
    await consumer.poll_once()
    assert consumer.diagnostics().payload_decode_failures == 1


# --------------------------------------------------------------------------- #
# B. Trading date gate
# --------------------------------------------------------------------------- #
async def test_stale_trading_date_is_rejected_and_terminally_acked() -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    transport.push(_raw(_envelope(_tick(), seq=1, trading_date=_TD - timedelta(days=1))))
    consumer = _consumer(transport, sink=sink)
    await consumer.poll_once()
    assert consumer.diagnostics().stale_trading_date_total == 1
    assert sink.applied_total == 0
    assert transport.pending == {}  # deterministic invalid -> ACK


async def test_future_trading_date_is_rejected_and_terminally_acked() -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    transport.push(_raw(_envelope(_tick(), seq=1, trading_date=_TD + timedelta(days=1))))
    consumer = _consumer(transport, sink=sink)
    await consumer.poll_once()
    assert consumer.diagnostics().future_trading_date_total == 1
    assert sink.applied_total == 0
    assert transport.pending == {}


async def test_current_trading_date_is_accepted() -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    transport.push(_raw(_envelope(_tick(), seq=1, trading_date=_TD)))
    consumer = _consumer(transport, sink=sink)
    await consumer.poll_once()
    assert sink.applied_total == 1


# --------------------------------------------------------------------------- #
# C. Universe version gate
# --------------------------------------------------------------------------- #
async def test_universe_match_is_accepted() -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    transport.push(_raw(_envelope(_tick(), seq=1, universe_version=7)))
    consumer = _consumer(transport, sink=sink, current_universe=7)
    await consumer.poll_once()
    assert sink.applied_total == 1


async def test_universe_older_is_rejected() -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    transport.push(_raw(_envelope(_tick(), seq=1, universe_version=6)))
    consumer = _consumer(transport, sink=sink, current_universe=7)
    await consumer.poll_once()
    assert consumer.diagnostics().older_universe_total == 1
    assert sink.applied_total == 0
    assert transport.pending == {}


async def test_universe_newer_is_rejected() -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    transport.push(_raw(_envelope(_tick(), seq=1, universe_version=9)))
    consumer = _consumer(transport, sink=sink, current_universe=7)
    await consumer.poll_once()
    assert consumer.diagnostics().newer_universe_total == 1
    assert sink.applied_total == 0


async def test_universe_unknown_is_rejected() -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    transport.push(_raw(_envelope(_tick(), seq=1, universe_version=7)))
    consumer = _consumer(transport, sink=sink, current_universe=None)
    await consumer.poll_once()
    assert consumer.diagnostics().unknown_universe_total == 1
    assert sink.applied_total == 0


# --------------------------------------------------------------------------- #
# D. Deduplication
# --------------------------------------------------------------------------- #
async def test_exact_replay_applies_exactly_once() -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    transport.push(_raw(_envelope(_tick(), seq=1)))
    transport.push(_raw(_envelope(_tick(), seq=1)))  # same identity
    consumer = _consumer(transport, sink=sink)
    await consumer.poll_once()
    assert sink.applied_total == 1
    assert consumer.diagnostics().duplicate_total == 1


async def test_same_sequence_different_epoch_is_distinct() -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    transport.push(_raw(_envelope(_tick(), seq=1, epoch=1)))
    transport.push(_raw(_envelope(_tick(), seq=1, epoch=2)))
    consumer = _consumer(transport, sink=sink)
    await consumer.poll_once()
    assert sink.applied_total == 2


async def test_same_sequence_different_producer_is_distinct() -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    transport.push(_raw(_envelope(_tick(), seq=1, producer="ingestion-a")))
    transport.push(_raw(_envelope(_tick(), seq=1, producer="ingestion-b")))
    consumer = _consumer(transport, sink=sink)
    await consumer.poll_once()
    assert sink.applied_total == 2


async def test_bounded_dedup_eviction_allows_reapply_outside_window() -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    dedup = BoundedDeduplicator(max_entries=2)
    consumer = _consumer(transport, sink=sink, dedup=dedup)
    for seq in (1, 2, 3):  # seq=1 evicted once window (2) overflows
        transport.push(_raw(_envelope(_tick(), seq=seq)))
    await consumer.poll_once()
    transport.push(_raw(_envelope(_tick(), seq=1)))  # replay of the evicted identity
    await consumer.poll_once()
    assert sink.applied_total == 4  # 3 distinct + 1 re-applied after eviction


# --------------------------------------------------------------------------- #
# E. ACK policy + failure isolation
# --------------------------------------------------------------------------- #
async def test_sink_failure_leaves_message_pending_and_is_retried() -> None:
    transport = FakeRawTransport()
    failing = _FailingSink(fail_times=1)
    transport.push(_raw(_envelope(_tick(), seq=1)))
    consumer = _consumer(transport, sink=failing)
    await consumer.poll_once()  # apply fails -> not acked, stays pending
    assert failing.applied_total == 0
    assert consumer.diagnostics().sink_failures == 1
    assert transport.pending != {}
    await consumer.poll_once()  # claim redelivers -> apply succeeds -> ack
    assert failing.applied_total == 1
    assert transport.pending == {}


async def test_successful_apply_then_ack_failure_does_not_double_apply() -> None:
    transport = FakeRawTransport()
    sink = RecordingShadowSink()
    transport.push(_raw(_envelope(_tick(), seq=1)))
    consumer = _consumer(transport, sink=sink)
    transport.fail_ack = True
    await consumer.poll_once()  # apply ok, dedup committed, ACK fails -> pending remains
    assert sink.applied_total == 1
    assert consumer.diagnostics().ack_failures == 1
    assert transport.pending != {}
    transport.fail_ack = False
    await consumer.poll_once()  # redelivery -> dedup hit -> ACK, no second apply
    assert sink.applied_total == 1
    assert transport.pending == {}


async def test_poison_message_terminally_acks() -> None:
    transport = FakeRawTransport()
    transport.push(b"garbage")
    consumer = _consumer(transport)
    await consumer.poll_once()
    assert transport.acked == ["0"]


# --------------------------------------------------------------------------- #
# G. Redis read failure isolation + diagnostics
# --------------------------------------------------------------------------- #
async def test_redis_read_failure_is_isolated_and_counted() -> None:
    transport = FakeRawTransport()
    transport.fail_read = True
    consumer = _consumer(transport)
    await consumer.poll_once()  # must not raise
    assert consumer.diagnostics().read_failures == 1


async def test_event_age_is_reported() -> None:
    transport = FakeRawTransport()
    transport.push(_raw(_envelope(_tick(), seq=1)))
    consumer = _consumer(transport, now=_NOW + timedelta(seconds=5))
    await consumer.poll_once()
    assert consumer.diagnostics().last_event_age_ms == pytest.approx(5_000.0)


async def test_diagnostics_carry_no_payload_or_credentials() -> None:
    transport = FakeRawTransport()
    transport.push(_raw(_envelope(_tick(price="123.45"), seq=1)))
    consumer = _consumer(transport)
    await consumer.poll_once()
    dumped = consumer.diagnostics().model_dump_json()
    assert "123.45" not in dumped  # no payload values leak into diagnostics
    assert "NSE:TCS" not in dumped  # no per-instrument identity in bounded health


class _FailingSink:
    """Sink that raises for the first ``fail_times`` applications, then records."""

    def __init__(self, fail_times: int) -> None:
        self._remaining = fail_times
        self.applied_total = 0

    async def apply(self, envelope: MarketEventEnvelope, event: IpcPayload) -> None:
        if self._remaining > 0:
            self._remaining -= 1
            raise RuntimeError("shadow sink transient failure")
        self.applied_total += 1
