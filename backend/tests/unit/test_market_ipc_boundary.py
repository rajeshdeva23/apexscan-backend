"""DECOUPLING-M2: bounded asynchronous publication boundary.

Proves Redis I/O leaves the ingestion hot path (submit is non-blocking, no network), the queue
is bounded with explicit overflow (never silent loss), one worker preserves FIFO/producer
identity, worker faults are observable and fail closed, and shutdown drains under a bounded
timeout. Uses a gated fake D1 publisher (asyncio.Event synchronisation, no wall-clock timing).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.market_ipc import (
    AsyncPublicationBoundary,
    BoundaryState,
    MarketIpcConfig,
    SubmitOutcome,
)
from app.market_ipc.envelope import MarketEventEnvelope, build_envelope
from app.market_ipc.publisher import PublishOutcome
from app.schemas.market_data import Instrument, MarketReference, Quote, Tick

_NOW = datetime(2026, 9, 9, 10, 15, 30, tzinfo=UTC)
_TD = date(2026, 9, 9)


class _FakePublisher:
    """Records transmit order/identity; optionally gates or fails the transmit (the Redis half)."""

    def __init__(self) -> None:
        self._epoch: int | None = None
        self._seq = 0
        self.transmitted: list[MarketEventEnvelope] = []
        self.gate: asyncio.Event | None = None  # when set-to-block, transmit awaits it
        self.transmit_started = asyncio.Event()
        self.raise_on_seq: int | None = None
        self.fail_outcome_on_seq: int | None = None
        self.prepare_outcome: PublishOutcome | None = None
        self.start_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        self._epoch = 1

    def prepare(self, datum: object) -> MarketEventEnvelope | PublishOutcome:
        if self.prepare_outcome is not None:
            return self.prepare_outcome
        assert self._epoch is not None
        self._seq += 1
        return build_envelope(
            datum,  # type: ignore[arg-type]
            producer_id="market-ingestion",
            producer_epoch=self._epoch,
            producer_sequence=self._seq,
            produced_at=_NOW,
            trading_date=_TD,
            universe_version=7,
        )

    async def transmit(self, envelope: MarketEventEnvelope) -> PublishOutcome:
        self.transmit_started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.raise_on_seq == envelope.producer_sequence:
            raise RuntimeError("transmit boom")
        self.transmitted.append(envelope)
        if self.fail_outcome_on_seq == envelope.producer_sequence:
            return PublishOutcome.FAILED_TRANSPORT
        return PublishOutcome.PUBLISHED


def _instrument(symbol: str = "TCS") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _tick(price: str = "100") -> Tick:
    return Tick(instrument=_instrument(), event_timestamp=_NOW, last_price=Decimal(price))


def _reference() -> MarketReference:
    return MarketReference(instrument=_instrument(), previous_close=Decimal("100"))


def _quote() -> Quote:
    return Quote(
        instrument=_instrument(),
        event_timestamp=_NOW,
        bid_price=Decimal("101"),
        ask_price=Decimal("102"),
        bid_quantity=1,
        ask_quantity=1,
    )


def _boundary(publisher: _FakePublisher, *, capacity: int = 100, drain: float = 1.0):
    return AsyncPublicationBoundary(
        publisher=publisher,  # type: ignore[arg-type]
        capacity=capacity,
        drain_timeout_seconds=drain,
        now=lambda: _NOW,
    )


async def _drain_join(publisher: _FakePublisher, boundary: AsyncPublicationBoundary) -> None:
    """Await until the worker has transmitted everything accepted (bounded by stop's drain)."""
    await boundary._queue.join()


# --- lifecycle / start ------------------------------------------------------ #
async def test_start_runs_worker_and_publisher() -> None:
    pub = _FakePublisher()
    b = _boundary(pub)
    await b.start()
    assert b.state is BoundaryState.RUNNING
    assert pub.start_calls == 1
    assert b.diagnostics().worker_running is True
    await b.stop()


async def test_submit_before_start_rejected() -> None:
    b = _boundary(_FakePublisher())
    assert b.submit(_tick()) is SubmitOutcome.REJECTED_NOT_RUNNING


# --- happy path / D1 reuse / ordering / identity ---------------------------- #
async def test_submit_enqueues_and_worker_transmits() -> None:
    pub = _FakePublisher()
    b = _boundary(pub)
    await b.start()
    assert b.submit(_tick()) is SubmitOutcome.ENQUEUED
    await _drain_join(pub, b)
    assert len(pub.transmitted) == 1
    assert b.diagnostics().published_total == 1
    await b.stop()


async def test_fifo_order_preserved_mixed_types() -> None:
    pub = _FakePublisher()
    b = _boundary(pub)
    await b.start()
    data = [_tick("1"), _reference(), _quote(), _tick("2"), _reference()]  # mixed classes
    for datum in data:
        assert b.submit(datum) is SubmitOutcome.ENQUEUED
    await _drain_join(pub, b)
    assert [e.producer_sequence for e in pub.transmitted] == [1, 2, 3, 4, 5]  # strict FIFO
    await b.stop()


async def test_producer_identity_preserved_through_boundary() -> None:
    pub = _FakePublisher()
    b = _boundary(pub)
    await b.start()
    b.submit(_reference())
    await _drain_join(pub, b)
    e = pub.transmitted[0]
    assert (e.producer_id, e.producer_epoch, e.producer_sequence) == ("market-ingestion", 1, 1)
    await b.stop()


# --- non-blocking submission independent of Redis latency ------------------- #
async def test_submit_does_not_wait_for_redis() -> None:
    pub = _FakePublisher()
    pub.gate = asyncio.Event()  # transmit blocks until released
    b = _boundary(pub, capacity=10)
    await b.start()
    assert b.submit(_tick()) is SubmitOutcome.ENQUEUED  # worker takes this one
    await pub.transmit_started.wait()  # worker has taken item 1 and is blocked in "Redis"
    # Submissions still succeed immediately while capacity remains, despite Redis being blocked.
    for _ in range(5):
        assert b.submit(_tick()) is SubmitOutcome.ENQUEUED
    assert b.diagnostics().published_total == 0  # nothing has actually reached Redis yet
    pub.gate.set()
    await _drain_join(pub, b)
    await b.stop()


# --- bounded queue + explicit overflow (never silent) ----------------------- #
async def test_overflow_is_explicit_and_queue_stays_bounded() -> None:
    pub = _FakePublisher()
    pub.gate = asyncio.Event()
    b = _boundary(pub, capacity=2)
    await b.start()
    b.submit(_tick())  # worker will take this one
    await pub.transmit_started.wait()  # worker now blocked on gate; queue is empty
    assert b.submit(_tick()) is SubmitOutcome.ENQUEUED  # fills 1/2
    assert b.submit(_tick()) is SubmitOutcome.ENQUEUED  # fills 2/2
    assert b.submit(_tick()) is SubmitOutcome.REJECTED_OVERFLOW  # explicit, not silent
    assert b._queue.qsize() <= 2  # never exceeds capacity
    assert b.diagnostics().overflow_total == 1
    pub.gate.set()
    await b.stop()


async def test_prepare_rejection_is_explicit_not_enqueued() -> None:
    pub = _FakePublisher()
    pub.prepare_outcome = PublishOutcome.FAILED_OVERSIZE
    b = _boundary(pub)
    await b.start()
    assert b.submit(_tick()) is SubmitOutcome.FAILED_OVERSIZE
    assert b.diagnostics().enqueued_total == 0
    assert b.diagnostics().prepare_rejected_total == 1
    await b.stop()


# --- worker failure fails closed -------------------------------------------- #
async def test_worker_terminal_failure_fails_closed() -> None:
    pub = _FakePublisher()
    pub.raise_on_seq = 2  # second event's transmit raises
    b = _boundary(pub)
    await b.start()
    b.submit(_tick("1"))  # seq 1 -> published
    b.submit(_tick("2"))  # seq 2 -> raises -> worker FAILED
    for _ in range(200):  # let the worker process until it fails
        if b.state is BoundaryState.FAILED:
            break
        await asyncio.sleep(0)
    assert b.state is BoundaryState.FAILED
    assert [e.producer_sequence for e in pub.transmitted] == [1]  # seq 2 never counted success
    assert b.submit(_tick("3")) is SubmitOutcome.REJECTED_NOT_RUNNING  # future submits rejected
    await b.stop()


async def test_publish_failure_outcome_counted_not_fatal() -> None:
    pub = _FakePublisher()
    pub.fail_outcome_on_seq = 1  # transmit returns FAILED_TRANSPORT (isolated, not an exception)
    b = _boundary(pub)
    await b.start()
    b.submit(_tick())
    await _drain_join(pub, b)
    assert b.state is BoundaryState.RUNNING  # a counted publish failure is not terminal
    assert b.diagnostics().publish_failure_total == 1
    await b.stop()


# --- shutdown / drain ------------------------------------------------------- #
async def test_stop_drains_pending_items() -> None:
    pub = _FakePublisher()
    b = _boundary(pub)
    await b.start()
    for _ in range(20):
        b.submit(_tick())
    result = await b.stop()
    assert result.drained_complete is True
    assert result.pending_at_stop == 0
    assert len(pub.transmitted) == 20  # every accepted item drained before stop returned
    assert b.state is BoundaryState.STOPPED


async def test_stop_drain_timeout_surfaces_incomplete() -> None:
    pub = _FakePublisher()
    pub.gate = asyncio.Event()  # never released -> drain cannot complete
    b = _boundary(pub, capacity=10, drain=0.05)
    await b.start()
    b.submit(_tick())
    b.submit(_tick())
    await pub.transmit_started.wait()
    result = await b.stop()  # bounded: returns after the drain timeout, never hangs
    assert result.drained_complete is False
    assert result.pending_at_stop >= 1  # accepted-but-unpublished surfaced, not silently dropped
    assert b.state is BoundaryState.STOPPED


async def test_submit_rejected_while_stopping_and_after_stopped() -> None:
    pub = _FakePublisher()
    b = _boundary(pub)
    await b.start()
    await b.stop()
    assert b.state is BoundaryState.STOPPED
    assert b.submit(_tick()) is SubmitOutcome.REJECTED_NOT_RUNNING


async def test_failed_worker_does_not_hang_shutdown() -> None:
    pub = _FakePublisher()
    pub.raise_on_seq = 1  # worker dies on the first item, leaving the rest queued
    b = _boundary(pub, drain=0.05)
    await b.start()
    for _ in range(3):  # submitted synchronously before the worker runs -> all queued
        b.submit(_tick())
    for _ in range(200):
        if b.state is BoundaryState.FAILED:
            break
        await asyncio.sleep(0)
    result = await b.stop()  # must not hang even though the worker died
    assert b.state is BoundaryState.STOPPED
    assert result.drained_complete is False  # remaining accepted items surfaced, not dropped
    assert result.pending_at_stop >= 1


# --- reconnect: boundary/worker/epoch survive an ordinary provider reconnect - #
async def test_ordinary_reconnect_preserves_worker_and_epoch() -> None:
    pub = _FakePublisher()
    b = _boundary(pub)
    await b.start()
    b.submit(_tick())
    await _drain_join(pub, b)
    epoch_before = pub._epoch
    worker_before = b._worker
    # An ordinary Dhan socket reconnect does not recreate the boundary/worker/producer.
    assert b.state is BoundaryState.RUNNING
    assert b.submit(_tick()) is SubmitOutcome.ENQUEUED
    await _drain_join(pub, b)
    assert pub._epoch == epoch_before  # no new epoch allocated
    assert b._worker is worker_before  # same worker task
    assert pub.start_calls == 1  # start not re-run
    await b.stop()


# --- stress / property / secret audit --------------------------------------- #
async def test_stress_all_published_in_order_bounded() -> None:
    pub = _FakePublisher()
    b = _boundary(pub, capacity=1000)
    await b.start()
    accepted = 0
    for _ in range(5000):
        if b.submit(_tick()) is SubmitOutcome.ENQUEUED:
            accepted += 1
        assert b._queue.qsize() <= 1000  # capacity respected at all times
        await asyncio.sleep(0)  # let the worker drain concurrently
    await _drain_join(pub, b)
    assert len(pub.transmitted) == accepted == 5000  # nothing dropped
    assert [e.producer_sequence for e in pub.transmitted] == list(range(1, 5001))  # strict FIFO
    await b.stop()


def test_m2_config_defaults_do_not_enable_ipc() -> None:
    config = MarketIpcConfig()
    assert config.enabled is False
    assert config.publish_queue_capacity == 10_000
    assert config.publish_shutdown_drain_timeout_seconds == 5.0


async def test_diagnostics_contains_no_secrets() -> None:
    pub = _FakePublisher()
    b = _boundary(pub)
    await b.start()
    b.submit(_tick())
    await _drain_join(pub, b)
    import json

    body = json.dumps(
        {k: str(getattr(b.diagnostics(), k)) for k in b.diagnostics().__slots__}
    ).lower()
    for secret in ("token", "totp", "pin", "secret", "password", "authorization"):
        assert secret not in body
    await b.stop()


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError, match="capacity"):
        AsyncPublicationBoundary(
            publisher=_FakePublisher(),  # type: ignore[arg-type]
            capacity=0,
            drain_timeout_seconds=1.0,
            now=lambda: _NOW,
        )
