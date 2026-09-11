"""DECOUPLING-D1: publisher routing STREAM_ONLY vs STREAM_PLUS_REFERENCE through the atomic path.

Unit-level proof (no Redis) that the publisher classifies reference-bearing events and routes
them to the atomic stream+reference primitive, routes non-reference events to the stream-only
path, records the reference outcome, and preserves failure isolation. The real atomicity is
proven against redislite in ``tests/integration/test_market_ipc_atomic_redis.py``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.market_ipc import (
    AtomicPublicationResult,
    MarketEventPublisher,
    MarketIpcConfig,
    ReferenceOutcome,
    StaticUniverseVersion,
)
from app.market_ipc.envelope import MarketEventEnvelope
from app.market_ipc.publisher import PublishOutcome
from app.market_ipc.transport import RedisPublishError
from app.schemas.market_data import Instrument, MarketReference, Quote

_NOW = datetime(2026, 9, 9, 10, 15, 30, tzinfo=UTC)
_TD = date(2026, 9, 9)


class _FakeStream:
    def __init__(self) -> None:
        self.published: list[MarketEventEnvelope] = []

    async def ensure_group(self) -> None:
        return None

    async def publish(self, envelope: MarketEventEnvelope) -> str:
        self.published.append(envelope)
        return str(len(self.published))


class _FakeAtomic:
    def __init__(self, reference_outcome: ReferenceOutcome = ReferenceOutcome.WRITTEN) -> None:
        self.stream_only_calls = 0
        self.stream_reference_calls = 0
        self._reference_outcome = reference_outcome
        self.raise_transport = False
        self.raise_oversize = False

    async def publish_stream_only(self, envelope: MarketEventEnvelope) -> AtomicPublicationResult:
        if self.raise_transport:
            raise RedisPublishError("down")
        if self.raise_oversize:
            raise ValueError("oversize")
        self.stream_only_calls += 1
        return AtomicPublicationResult("1", ReferenceOutcome.NO_REFERENCE_DATA)

    async def publish_stream_and_reference(
        self, envelope: MarketEventEnvelope, reference_state: object
    ) -> AtomicPublicationResult:
        if self.raise_transport:
            raise RedisPublishError("down")
        if self.raise_oversize:
            raise ValueError("oversize")
        self.stream_reference_calls += 1
        return AtomicPublicationResult("1", self._reference_outcome)


class _FakeEpoch:
    async def allocate(self, producer_id: str) -> int:
        return 1


def _publisher(stream: _FakeStream, atomic: _FakeAtomic | None) -> MarketEventPublisher:
    return MarketEventPublisher(
        stream=stream,
        config=MarketIpcConfig(),
        producer_id="market-ingestion",
        epoch_allocator=_FakeEpoch(),
        trading_date_source=type("_D", (), {"current_trading_date": lambda self: _TD})(),
        universe_version_source=StaticUniverseVersion(7),
        now=lambda: _NOW,
        atomic_publisher=atomic,
    )


def _instrument() -> Instrument:
    return Instrument(exchange="NSE", symbol="TCS")


def _quote() -> Quote:
    return Quote(
        instrument=_instrument(),
        event_timestamp=_NOW,
        bid_price=Decimal("101"),
        ask_price=Decimal("102"),
        bid_quantity=1,
        ask_quantity=1,
    )


def _reference() -> MarketReference:
    return MarketReference(instrument=_instrument(), previous_close=Decimal("100.5"))


async def test_non_reference_event_routes_stream_only() -> None:
    stream, atomic = _FakeStream(), _FakeAtomic()
    pub = _publisher(stream, atomic)
    await pub.start()
    assert await pub.publish(_quote()) is PublishOutcome.PUBLISHED
    assert atomic.stream_only_calls == 1
    assert atomic.stream_reference_calls == 0
    assert stream.published == []  # atomic path used, not the plain stream
    assert pub.diagnostics().stream_only_total == 1


async def test_reference_event_routes_stream_plus_reference() -> None:
    stream, atomic = _FakeStream(), _FakeAtomic()
    pub = _publisher(stream, atomic)
    await pub.start()
    assert await pub.publish(_reference()) is PublishOutcome.PUBLISHED
    assert atomic.stream_reference_calls == 1
    assert atomic.stream_only_calls == 0
    diag = pub.diagnostics()
    assert diag.stream_reference_total == 1
    assert diag.reference_noop_total == 0


async def test_reference_noop_counted_for_stale_or_duplicate() -> None:
    for outcome in (ReferenceOutcome.STALE_REJECTED, ReferenceOutcome.DUPLICATE):
        stream = _FakeStream()
        atomic = _FakeAtomic(reference_outcome=outcome)
        pub = _publisher(stream, atomic)
        await pub.start()
        assert await pub.publish(_reference()) is PublishOutcome.PUBLISHED
        assert pub.diagnostics().reference_noop_total == 1


async def test_atomic_transport_failure_is_isolated() -> None:
    stream, atomic = _FakeStream(), _FakeAtomic()
    atomic.raise_transport = True
    pub = _publisher(stream, atomic)
    await pub.start()
    assert await pub.publish(_reference()) is PublishOutcome.FAILED_TRANSPORT  # never raises
    assert pub.diagnostics().publish_failures_total == 1


async def test_atomic_oversize_reencode_is_isolated() -> None:
    stream, atomic = _FakeStream(), _FakeAtomic()
    atomic.raise_oversize = True  # a re-encode ValueError must not escape publish()
    pub = _publisher(stream, atomic)
    await pub.start()
    assert await pub.publish(_reference()) is PublishOutcome.FAILED_OVERSIZE
    assert pub.diagnostics().oversize_rejections_total == 1


async def test_without_atomic_publisher_uses_plain_stream() -> None:
    stream = _FakeStream()
    pub = _publisher(stream, None)  # backward-compatible Phase-B behaviour
    await pub.start()
    assert await pub.publish(_reference()) is PublishOutcome.PUBLISHED
    assert len(stream.published) == 1  # plain stream append, no reference projection
