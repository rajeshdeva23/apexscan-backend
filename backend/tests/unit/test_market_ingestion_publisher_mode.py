"""Unit tests for H3A publisher-mode wiring and fail-closed behaviour (DECOUPLING PHASE H3A).

Deterministic (no Redis): the publishing sink's submit-outcome mapping, and the service's
fail-closed actuation — provider never starts if the publication infra fails, an overflow at the
sink stops intake and disconnects the provider, the observer trips on a worker fault, and a clean
stop drains M2 and finalizes L1. The end-to-end happy path is proven against real Redis in the
integration suite.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.market_ingestion.errors import PublicationTerminalError
from app.market_ingestion.mode import PhaseHFlags
from app.market_ingestion.publication import PublicationStack, PublishingEventSink
from app.market_ingestion.service import MarketIngestionService, ServiceStatus
from app.market_ipc.boundary import BoundaryDiagnostics, BoundaryState, DrainResult, SubmitOutcome
from app.market_ipc.continuity import ContinuityState, FeedContinuityTracker
from app.schemas.market_data import (
    Instrument,
    MarketData,
    MarketDataKind,
    ProviderHealth,
    ProviderStatus,
    SubscriptionRequest,
    Tick,
)

_NOW = datetime(2026, 9, 13, 4, 0, tzinfo=UTC)


def _tick() -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol="TCS"),
        event_timestamp=_NOW,
        last_price=Decimal("100.5"),
    )


def _flags() -> PhaseHFlags:
    return PhaseHFlags(
        market_ingestion_service_enabled=True,
        ipc_publisher_enabled=True,
        ipc_consumer_enabled=False,
        ipc_shadow_compare_enabled=False,
        ipc_authoritative_enabled=False,
        legacy_market_path_enabled=True,
    )


def _request() -> SubscriptionRequest:
    return SubscriptionRequest(
        instruments=(Instrument(exchange="NSE", symbol="TCS"),),
        data_types=frozenset({MarketDataKind.TICK}),
    )


async def _yield_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)  # yield to the loop (never starve the observer poll)


class _FakeBoundary:
    """Controllable M2 boundary double (start error, submit outcome, diagnostics state)."""

    def __init__(
        self,
        *,
        start_error: Exception | None = None,
        submit_outcome: SubmitOutcome = SubmitOutcome.ENQUEUED,
        state: BoundaryState = BoundaryState.RUNNING,
    ) -> None:
        self._start_error = start_error
        self._submit_outcome = submit_outcome
        self._state = state
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True
        if self._start_error is not None:
            raise self._start_error

    def submit(self, datum: MarketData) -> SubmitOutcome:
        return self._submit_outcome

    def diagnostics(self) -> BoundaryDiagnostics:
        return BoundaryDiagnostics(
            state=self._state,
            queue_depth=0,
            queue_capacity=10,
            queue_high_watermark=0,
            enqueued_total=0,
            published_total=0,
            publish_failure_total=0,
            overflow_total=0,
            prepare_rejected_total=0,
            worker_running=self._state is BoundaryState.RUNNING,
            last_failure=None,
            last_publish_success_at=None,
            last_publish_failure_at=None,
        )

    async def stop(self) -> DrainResult:
        self.stopped = True
        return DrainResult(drained_complete=True, pending_at_stop=0)


async def _aclose() -> None:
    """Async no-op close for the fake Redis in the publisher stack (no real I/O here)."""


def _stack(boundary: _FakeBoundary) -> PublicationStack:
    tracker = FeedContinuityTracker()
    publisher = SimpleNamespace(
        diagnostics=lambda: SimpleNamespace(producer_epoch=7), current_sequence=1
    )
    sink = PublishingEventSink(
        boundary=boundary,  # type: ignore[arg-type]
        continuity=tracker,
        publisher=publisher,  # type: ignore[arg-type]
    )
    return PublicationStack(
        producer_id="market-ingestion",
        publisher=publisher,  # type: ignore[arg-type]
        boundary=boundary,  # type: ignore[arg-type]
        continuity=tracker,
        sink=sink,
        redis=SimpleNamespace(aclose=_aclose),  # type: ignore[arg-type]  # no real Redis I/O
    )


class _FakeProvider:
    """Provider double emitting a configurable episode; records connect/disconnect ordering."""

    def __init__(self, events: list[MarketData], *, order: list[str] | None = None) -> None:
        self._events = events
        self._order = order
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        if self._order is not None:
            self._order.append("provider.connect")

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        for event in self._events:
            yield event
        await asyncio.sleep(0)  # let a terminal raise from the sink settle deterministically


def _service(boundary: _FakeBoundary, provider: _FakeProvider) -> MarketIngestionService:
    return MarketIngestionService(
        flags=_flags(),
        provider=provider,  # type: ignore[arg-type]
        subscription_request=_request(),
        publication=_stack(boundary),
        supervisor_max_reconnects=0,
        supervisor_sleep=_yield_sleep,
        observer_interval_seconds=0.0,
        observer_sleep=_yield_sleep,
    )


# --------------------------------------------------------------------------- #
# PublishingEventSink mapping
# --------------------------------------------------------------------------- #
def test_sink_enqueued_records_accepted_position() -> None:
    tracker = FeedContinuityTracker()
    tracker.producer_started(producer_id="p", producer_epoch=1)
    publisher = SimpleNamespace(current_sequence=5)
    sink = PublishingEventSink(
        boundary=_FakeBoundary(submit_outcome=SubmitOutcome.ENQUEUED),  # type: ignore[arg-type]
        continuity=tracker,
        publisher=publisher,  # type: ignore[arg-type]
    )
    sink.handle(_tick())
    assert tracker.snapshot().last_accepted_sequence == 5


def test_accepted_position_leads_published_under_backlog() -> None:
    # Enqueue is not Redis durability: with the worker not draining (published_total stays 0), the
    # accepted position advances ahead of confirmed publication.
    tracker = FeedContinuityTracker()
    tracker.producer_started(producer_id="p", producer_epoch=1)
    counter = SimpleNamespace(current_sequence=0)
    boundary = _FakeBoundary(submit_outcome=SubmitOutcome.ENQUEUED)  # published_total == 0
    sink = PublishingEventSink(
        boundary=boundary,  # type: ignore[arg-type]
        continuity=tracker,
        publisher=counter,  # type: ignore[arg-type]
    )
    for seq in (1, 2, 3):
        counter.current_sequence = seq
        sink.handle(_tick())
    assert tracker.snapshot().last_accepted_sequence == 3  # accepted
    assert boundary.diagnostics().published_total == 0  # nothing confirmed published yet


def test_sink_overflow_is_terminal() -> None:
    tracker = FeedContinuityTracker()
    tracker.producer_started(producer_id="p", producer_epoch=1)
    sink = PublishingEventSink(
        boundary=_FakeBoundary(submit_outcome=SubmitOutcome.REJECTED_OVERFLOW),  # type: ignore[arg-type]
        continuity=tracker,
        publisher=SimpleNamespace(current_sequence=1),  # type: ignore[arg-type]
    )
    with pytest.raises(PublicationTerminalError):
        sink.handle(_tick())
    assert tracker.state is ContinuityState.BROKEN


def test_sink_not_running_is_terminal() -> None:
    tracker = FeedContinuityTracker()
    tracker.producer_started(producer_id="p", producer_epoch=1)
    sink = PublishingEventSink(
        boundary=_FakeBoundary(submit_outcome=SubmitOutcome.REJECTED_NOT_RUNNING),  # type: ignore[arg-type]
        continuity=tracker,
        publisher=SimpleNamespace(current_sequence=1),  # type: ignore[arg-type]
    )
    with pytest.raises(PublicationTerminalError):
        sink.handle(_tick())


def test_sink_prepare_reject_is_not_terminal() -> None:
    tracker = FeedContinuityTracker()
    tracker.producer_started(producer_id="p", producer_epoch=1)
    sink = PublishingEventSink(
        boundary=_FakeBoundary(submit_outcome=SubmitOutcome.FAILED_OVERSIZE),  # type: ignore[arg-type]
        continuity=tracker,
        publisher=SimpleNamespace(current_sequence=1),  # type: ignore[arg-type]
    )
    sink.handle(_tick())  # must not raise
    assert tracker.state is ContinuityState.HEALTHY
    assert tracker.snapshot().prepare_rejected_total == 1


# --------------------------------------------------------------------------- #
# Service publisher-mode lifecycle (fakes)
# --------------------------------------------------------------------------- #
async def test_provider_never_starts_if_publication_infra_fails() -> None:
    boundary = _FakeBoundary(start_error=ConnectionError("redis down"))
    provider = _FakeProvider([_tick()])
    service = _service(boundary, provider)
    with pytest.raises(ConnectionError):
        await service.start()
    assert service.status is ServiceStatus.FAILED
    assert boundary.started is True
    assert provider.connect_calls == 0  # provider never started without publication infra


async def test_startup_order_boundary_before_provider() -> None:
    order: list[str] = []
    boundary = _FakeBoundary()
    original_start = boundary.start

    async def _record_start() -> None:
        order.append("boundary.start")
        await original_start()

    boundary.start = _record_start  # type: ignore[method-assign]
    provider = _FakeProvider([], order=order)
    service = _service(boundary, provider)
    await service.start()
    assert order[:2] == ["boundary.start", "provider.connect"]  # publication infra first
    await service.stop()


async def test_overflow_fails_closed_and_disconnects_provider() -> None:
    boundary = _FakeBoundary(submit_outcome=SubmitOutcome.REJECTED_OVERFLOW)
    provider = _FakeProvider([_tick()])
    service = _service(boundary, provider)
    await service.start()
    await asyncio.wait_for(service._watch_task, timeout=1.0)  # terminal → fail-closed settles
    assert service.status is ServiceStatus.FAILED
    assert provider.disconnect_calls >= 1  # provider intake stopped + disconnected
    assert service.diagnostics().continuity_state == "broken"


async def test_observer_trips_on_worker_fault() -> None:
    boundary = _FakeBoundary(state=BoundaryState.FAILED)
    service = _service(boundary, _FakeProvider([]))
    service._publication.continuity.producer_started(  # type: ignore[union-attr]
        producer_id="market-ingestion", producer_epoch=7
    )
    await service._run_observer()  # one pass detects FAILED and trips the terminal signal
    assert service._terminal.is_set()
    assert service._publication.continuity.state is ContinuityState.BROKEN  # type: ignore[union-attr]


async def test_clean_stop_drains_and_finalizes_l1() -> None:
    boundary = _FakeBoundary()
    service = _service(boundary, _FakeProvider([]))
    await service.start()
    await service.stop()
    assert service.status is ServiceStatus.STOPPED
    assert boundary.stopped is True
    assert service.diagnostics().continuity_state == "stopped"
