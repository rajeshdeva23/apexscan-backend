"""Shadow IPC publisher: identity/epoch/sequence, ordering, failure isolation, attachment (PHASE B).

Component tests use the Phase-A in-memory stream + a fake INCR-style epoch allocator; runtime
tests prove the publisher is off by default, that a present publisher receives canonical data,
and that a publisher start failure degrades safely without disrupting the authoritative path.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

from app.core.config import Settings
from app.market_engine.clock import ManualClock
from app.market_engine.sequence import MonotonicSequence
from app.market_ipc import (
    InMemoryMarketEventStream,
    MarketEventPublisher,
    MarketIpcConfig,
    PublishOutcome,
    StaticUniverseVersion,
    decode_payload,
)
from app.market_ipc.transport import RedisPublishError
from app.schemas.market_data import (
    Candle,
    FeedContinuity,
    FeedContinuityEvent,
    Instrument,
    MarketData,
    MarketReference,
    ProviderSessionOhlc,
    Quote,
    SubscriptionRequest,
    Tick,
)
from app.services.market_runtime import LiveMarketRuntime

_IST = timezone(timedelta(hours=5, minutes=30))
_NOW = datetime(2026, 9, 9, 10, 15, 30, tzinfo=_IST)
_TD = date(2026, 9, 9)
_PRODUCER = "market-ingestion"


class _FakeEpoch:
    """INCR-style allocator: monotonic, a new value per call (i.e. per producer start)."""

    def __init__(self) -> None:
        self.calls = 0

    async def allocate(self, producer_id: str) -> int:
        assert producer_id == _PRODUCER
        self.calls += 1
        return self.calls


class _FixedDate:
    def current_trading_date(self) -> date:
        return _TD


class _FailingStream(InMemoryMarketEventStream):
    async def publish(self, envelope: object) -> str:  # type: ignore[override]
        raise RedisPublishError("redis down")


def _instrument(symbol: str = "TCS") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _tick(symbol: str = "TCS", *, price: str = "3456.75") -> Tick:
    last = Decimal(price)
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=_NOW,
        last_price=last,
        session_ohlc=ProviderSessionOhlc(
            open_price=last,
            high_price=last + Decimal("5"),
            low_price=last - Decimal("5"),
            close_price=last,
        ),
    )


def _publisher(
    stream: InMemoryMarketEventStream,
    *,
    epoch: _FakeEpoch | None = None,
    config: MarketIpcConfig | None = None,
    universe: int = 7,
) -> MarketEventPublisher:
    return MarketEventPublisher(
        stream=stream,
        config=config or MarketIpcConfig(),
        producer_id=_PRODUCER,
        epoch_allocator=epoch or _FakeEpoch(),
        trading_date_source=_FixedDate(),
        universe_version_source=StaticUniverseVersion(universe),
        now=lambda: _NOW,
    )


async def _started(stream: InMemoryMarketEventStream, **kw: object) -> MarketEventPublisher:
    pub = _publisher(stream, **kw)  # type: ignore[arg-type]
    await pub.start()
    return pub


# --------------------------------------------------------------------------- #
# round-trips for every supported kind
# --------------------------------------------------------------------------- #
async def test_publishes_all_supported_kinds_losslessly() -> None:
    stream = InMemoryMarketEventStream()
    pub = await _started(stream)
    payloads: list[MarketData] = [
        _tick(),
        Quote(
            instrument=_instrument(),
            event_timestamp=_NOW,
            bid_price=Decimal("3456.50"),
            ask_price=Decimal("3456.90"),
            bid_quantity=300,
            ask_quantity=150,
        ),
        MarketReference(instrument=_instrument(), previous_close=Decimal("3410.25")),
        FeedContinuityEvent(status=FeedContinuity.CONNECTED, observed_at=_NOW),
    ]
    for datum in payloads:
        assert await pub.publish(datum) is PublishOutcome.PUBLISHED
    delivered = await stream.read()
    assert len(delivered) == 4
    for (_mid, env), original in zip(delivered, payloads, strict=True):
        assert decode_payload(env.event_kind, env.payload) == original


async def test_envelope_metadata_is_correct_and_broker_neutral() -> None:
    stream = InMemoryMarketEventStream()
    pub = await _started(stream, universe=7)
    await pub.publish(_tick())
    _mid, env = (await stream.read())[0]
    assert env.producer_id == _PRODUCER
    assert env.producer_epoch == 1
    assert env.producer_sequence == 1  # first sequence within an epoch is 1
    assert env.trading_date == _TD
    assert env.universe_version == 7
    assert env.instrument_identity == "NSE:TCS"  # no provider security id
    assert env.produced_at == _NOW.astimezone(UTC)  # tz-aware, normalized to UTC
    tick = decode_payload(env.event_kind, env.payload)
    assert tick.last_price == Decimal("3456.75")  # Decimal lossless


async def test_producer_sequence_is_monotonic_within_epoch() -> None:
    stream = InMemoryMarketEventStream()
    pub = await _started(stream)
    for _ in range(5):
        await pub.publish(_tick())
    seqs = [env.producer_sequence for _mid, env in await stream.read()]
    assert seqs == [1, 2, 3, 4, 5]


# --------------------------------------------------------------------------- #
# epoch: restart uniqueness + sequence reset
# --------------------------------------------------------------------------- #
async def test_restart_gets_new_epoch_and_resets_sequence() -> None:
    stream = InMemoryMarketEventStream()
    epoch = _FakeEpoch()
    first = await _started(stream, epoch=epoch)
    await first.publish(_tick())
    second = await _started(stream, epoch=epoch)  # simulates a producer restart
    await second.publish(_tick())
    envs = [env for _mid, env in await stream.read()]
    assert (envs[0].producer_epoch, envs[0].producer_sequence) == (1, 1)
    assert (envs[1].producer_epoch, envs[1].producer_sequence) == (2, 1)  # new epoch, seq resets


async def test_start_is_idempotent_and_allocates_one_epoch() -> None:
    epoch = _FakeEpoch()
    pub = await _started(InMemoryMarketEventStream(), epoch=epoch)
    await pub.start()  # second call is a no-op
    assert epoch.calls == 1


# --------------------------------------------------------------------------- #
# failure isolation + deterministic sequence-on-failure
# --------------------------------------------------------------------------- #
async def test_transport_failure_is_isolated_and_consumes_the_sequence() -> None:
    pub = await _started(_FailingStream())
    assert await pub.publish(_tick()) is PublishOutcome.FAILED_TRANSPORT  # never raises
    diag = pub.diagnostics()
    assert diag.publish_failures_total == 1
    assert diag.events_published_total == 0
    assert diag.current_sequence == 1  # advance-on-failure: sequence consumed, never reused


async def test_failed_then_next_event_does_not_reuse_identity() -> None:
    pub = await _started(_FailingStream())
    await pub.publish(_tick())  # seq 1 fails
    await pub.publish(_tick())  # seq 2, not a reuse of 1
    assert pub.diagnostics().current_sequence == 2


async def test_unsupported_type_is_rejected_without_raising() -> None:
    stream = InMemoryMarketEventStream()
    pub = await _started(stream)
    candle = Candle(
        instrument=_instrument(),
        start_timestamp=_NOW,
        end_timestamp=_NOW + timedelta(minutes=1),
        open_price=Decimal("100"),
        high_price=Decimal("110"),
        low_price=Decimal("90"),
        close_price=Decimal("105"),
        traded_quantity=10,
    )
    assert await pub.publish(candle) is PublishOutcome.FAILED_UNSUPPORTED
    assert pub.diagnostics().unsupported_type_total == 1
    assert await stream.read() == []  # nothing published


async def test_oversized_payload_is_rejected() -> None:
    stream = InMemoryMarketEventStream()
    pub = await _started(stream, config=MarketIpcConfig(max_payload_bytes=256))
    assert await pub.publish(_tick()) is PublishOutcome.FAILED_OVERSIZE
    assert pub.diagnostics().oversize_rejections_total == 1
    assert await stream.read() == []


# --------------------------------------------------------------------------- #
# ordering
# --------------------------------------------------------------------------- #
async def test_global_publish_order_preserved_across_instruments() -> None:
    stream = InMemoryMarketEventStream()
    pub = await _started(stream)
    order = [f"SYM{i % 7}" for i in range(100)]
    for symbol in order:
        await pub.publish(_tick(symbol))
    delivered = await stream.read()
    assert [env.instrument_identity for _mid, env in delivered] == [f"NSE:{s}" for s in order]
    assert [env.producer_sequence for _mid, env in delivered] == list(range(1, 101))


# --------------------------------------------------------------------------- #
# security
# --------------------------------------------------------------------------- #
async def test_published_envelope_carries_no_credentials_or_provider_ids() -> None:
    stream = InMemoryMarketEventStream()
    pub = await _started(stream)
    await pub.publish(_tick())
    from app.market_ipc import encode_envelope

    _mid, env = (await stream.read())[0]
    wire = encode_envelope(env).decode().lower()
    for forbidden in ("token", "totp", "password", "authorization", "client_id", "security_id"):
        assert forbidden not in wire


# --------------------------------------------------------------------------- #
# runtime attachment: off by default, active when present, safe on start failure
# --------------------------------------------------------------------------- #
_DB = "postgresql+asyncpg://user:pass@localhost:5432/apexscan"
_REDIS = "redis://localhost:6379/0"


class _FakeLive:
    def __init__(self, events: tuple[MarketData, ...]) -> None:
        self._events = events
        self._gate = asyncio.Event()
        self.drained = asyncio.Event()

    def bind_continuity(self, sink: Callable[[FeedContinuityEvent], None]) -> None:
        return None

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        for event in self._events:
            yield event
        self.drained.set()
        await self._gate.wait()


def _runtime(live: _FakeLive, publisher: MarketEventPublisher | None) -> LiveMarketRuntime:
    return LiveMarketRuntime(
        settings=Settings(app_env="development", database_url=_DB, redis_url=_REDIS),
        error_threshold=3,
        instruments=(_instrument("RELIANCE"),),
        live_market_data=live,
        clock=ManualClock(_NOW.astimezone(UTC)),
        sequence=MonotonicSequence(),
        market_event_publisher=publisher,
    )


async def _drain(live: _FakeLive) -> None:
    for _ in range(1000):
        if live.drained.is_set():
            return
        await asyncio.sleep(0)
    raise AssertionError("stream did not drain")


async def test_runtime_without_publisher_is_inert() -> None:
    stream = InMemoryMarketEventStream()  # held externally; runtime gets no publisher
    live = _FakeLive((_tick("RELIANCE"),))
    runtime = _runtime(live, publisher=None)
    await runtime.start()
    await _drain(live)
    await asyncio.sleep(0)
    assert await stream.read() == []  # nothing published: authoritative path only
    await runtime.shutdown()


async def test_runtime_with_publisher_shadow_publishes() -> None:
    stream = InMemoryMarketEventStream()
    live = _FakeLive((_tick("RELIANCE"), _tick("RELIANCE", price="101.5")))
    runtime = _runtime(live, publisher=_publisher(stream))
    await runtime.start()
    await _drain(live)
    await asyncio.sleep(0)
    delivered = await stream.read()
    assert len(delivered) == 2
    assert all(env.instrument_identity == "NSE:RELIANCE" for _mid, env in delivered)
    await runtime.shutdown()


async def test_publisher_start_failure_degrades_without_breaking_runtime() -> None:
    class _BadEpoch:
        async def allocate(self, producer_id: str) -> int:
            raise RedisPublishError("redis unavailable at startup")

    stream = InMemoryMarketEventStream()
    live = _FakeLive((_tick("RELIANCE"),))
    publisher = _publisher(stream, epoch=_BadEpoch())  # type: ignore[arg-type]
    runtime = _runtime(live, publisher=publisher)
    await runtime.start()  # must not raise
    await _drain(live)  # authoritative ingestion still runs
    assert await stream.read() == []  # publisher disabled, nothing shadow-published
    await runtime.shutdown()
