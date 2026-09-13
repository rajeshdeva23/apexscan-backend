"""Failure/recovery-path hardening for the H3A shadow-publish pipeline (DECOUPLING PHASE H3B).

H3A proved the happy path. These deterministic tests drive the *failure* matrix against the REAL
M2 :class:`AsyncPublicationBoundary` and REAL L1 :class:`FeedContinuityTracker` (only the D1
publisher is a controllable double, so there is no Redis and no timing on real I/O): terminal
publication breaks fail closed, the terminal path bypasses the reconnect self-heal, shutdown/
reconnect races stay idempotent, an unknown/definite publication outcome is never counted as
success, and a clean vs incomplete drain is reported honestly. Real Dhan is never contacted.

Determinism note: a terminal break propagated by the *supervisor* (overflow → PublicationTerminal
Error) sets the service's terminal signal via the supervisor task's done-callback before ``wait``
resumes, so those assertions need no polling. A worker fault / FAILED_TRANSPORT is detected by the
bounded L1 *observer* poll, so those tests await ``service._watch_task`` under a timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.market_ingestion.errors import PublicationTerminalError
from app.market_ingestion.mode import PhaseHFlags
from app.market_ingestion.publication import PublicationStack, PublishingEventSink
from app.market_ingestion.service import MarketIngestionService, ServiceStatus
from app.market_ipc.boundary import AsyncPublicationBoundary, BoundaryState
from app.market_ipc.continuity import ContinuityReason, ContinuityState, FeedContinuityTracker
from app.market_ipc.publisher import PublishOutcome
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
_ENVELOPE = object()  # opaque token: the boundary enqueues it and hands it back to transmit()


def _tick(symbol: str = "TCS") -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol=symbol),
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


async def _until(predicate: object, *, limit: int = 100_000) -> None:
    """Bounded wait for a predicate; raise rather than hang if it never holds."""
    for _ in range(limit):
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was never reached")


class _FakePublisher:
    """Controllable M2 publisher double: honours the prepare/transmit contract, no Redis.

    ``transmit`` can publish, return a terminal outcome, raise, or block on a gate; ``prepare``
    can reject a chosen symbol (advance-on-failure still consumes a sequence, like the real one).
    """

    def __init__(
        self,
        *,
        epoch: int = 1,
        transmit_outcome: PublishOutcome = PublishOutcome.PUBLISHED,
        transmit_error: Exception | None = None,
        transmit_gate: asyncio.Event | None = None,
        reject_symbols: frozenset[str] = frozenset(),
    ) -> None:
        self._epoch_value = epoch
        self._epoch: int | None = None
        self._seq = 0
        self._transmit_outcome = transmit_outcome
        self._transmit_error = transmit_error
        self._transmit_gate = transmit_gate
        self._reject_symbols = reject_symbols
        self.start_calls = 0
        self.transmit_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        self._epoch = self._epoch_value
        self._seq = 0

    @property
    def current_sequence(self) -> int:
        return self._seq

    def prepare(self, datum: MarketData) -> object:
        if self._epoch is None:
            raise RuntimeError("publisher used before start()")
        self._seq += 1  # advance-on-failure: a rejected prepare still burns a sequence
        if getattr(datum.instrument, "symbol", None) in self._reject_symbols:
            return PublishOutcome.FAILED_OVERSIZE
        return _ENVELOPE

    async def transmit(self, _envelope: object) -> PublishOutcome:
        self.transmit_calls += 1
        if self._transmit_gate is not None:
            await self._transmit_gate.wait()
        if self._transmit_error is not None:
            raise self._transmit_error
        return self._transmit_outcome

    def diagnostics(self) -> SimpleNamespace:
        return SimpleNamespace(producer_epoch=self._epoch)


class _StartFailPublisher(_FakePublisher):
    """Publisher whose start() fails closed (Redis/epoch unavailable analogue)."""

    async def start(self) -> None:
        self.start_calls += 1
        raise ConnectionError("publication infra unavailable")


class _Provider:
    """Provider double: yields one episode of events then ends. Records connect/disconnect."""

    def __init__(self, events: list[MarketData]) -> None:
        self._events = events
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.stream_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def stream_market_data(self, _request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        self.stream_calls += 1
        for event in self._events:
            yield event
        await asyncio.sleep(0)  # let a terminal raise from the sink settle deterministically


class _UnhealthyProvider(_Provider):
    """Connects, but reports UNHEALTHY so the coordinator fails the start."""

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.UNHEALTHY, observed_at=_NOW)


class _DropThenBlockProvider:
    """Episode 1 yields a tick then raises (recoverable drop); later episodes block on a gate."""

    def __init__(self, event: MarketData, reconnect_gate: asyncio.Event) -> None:
        self._event = event
        self._reconnect_gate = reconnect_gate
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.stream_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def stream_market_data(self, _request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        self.stream_calls += 1
        if self.stream_calls == 1:
            yield self._event
            raise ConnectionError("simulated transport drop")  # recoverable → reconnect
        await self._reconnect_gate.wait()  # a reconnect episode that never yields (blocked)
        yield self._event


class _BlockingProvider:
    """Streams a tick then blocks forever (until cancelled) — for cancellation tests."""

    def __init__(self, event: MarketData, block: asyncio.Event) -> None:
        self._event = event
        self._block = block
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.stream_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def get_health(self) -> ProviderHealth:
        return ProviderHealth(status=ProviderStatus.HEALTHY, observed_at=_NOW)

    async def stream_market_data(self, _request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        self.stream_calls += 1
        yield self._event
        await self._block.wait()  # hold the stream open until the test releases it


def _stack(
    publisher: _FakePublisher, *, capacity: int = 16, drain_timeout: float = 1.0
) -> PublicationStack:
    """A real M2 boundary + real L1 tracker in front of the controllable publisher double."""
    boundary = AsyncPublicationBoundary(
        publisher=publisher,  # type: ignore[arg-type]
        capacity=capacity,
        drain_timeout_seconds=drain_timeout,
        now=lambda: _NOW,
    )
    continuity = FeedContinuityTracker()
    sink = PublishingEventSink(
        boundary=boundary,
        continuity=continuity,
        publisher=publisher,  # type: ignore[arg-type]
    )
    return PublicationStack(
        producer_id="market-ingestion",
        publisher=publisher,  # type: ignore[arg-type]
        boundary=boundary,
        continuity=continuity,
        sink=sink,
    )


def _service(
    stack: PublicationStack,
    provider: object,
    *,
    max_reconnects: int | None = 0,
    supervisor_sleep: object = _yield_sleep,
) -> MarketIngestionService:
    return MarketIngestionService(
        flags=_flags(),
        provider=provider,  # type: ignore[arg-type]
        subscription_request=_request(),
        publication=stack,
        supervisor_max_reconnects=max_reconnects,
        supervisor_sleep=supervisor_sleep,  # type: ignore[arg-type]
        observer_interval_seconds=0.0,
        observer_sleep=_yield_sleep,
    )


# --------------------------------------------------------------------------- #
# Startup fail-closed (F1 / F16 / F17)
# --------------------------------------------------------------------------- #
async def test_publication_infra_failure_before_provider_start(  # F1
) -> None:
    publisher = _StartFailPublisher()
    provider = _Provider([_tick()])
    service = _service(_stack(publisher), provider)
    with pytest.raises(ConnectionError):
        await service.start()
    assert service.status is ServiceStatus.FAILED
    assert publisher.start_calls == 1
    assert provider.connect_calls == 0  # provider never contacted without publication infra
    assert service.terminal_failure is False  # a startup infra failure is not a terminal break
    assert service._observer_task is None and service._watch_task is None  # no leaked tasks


async def test_failed_start_after_m2_started_cleans_up(  # F17
) -> None:
    # Boundary starts (epoch allocated, observer/watch spawned), then the provider is unhealthy.
    publisher = _FakePublisher()
    provider = _UnhealthyProvider([_tick()])
    stack = _stack(publisher)
    service = _service(stack, provider)
    with pytest.raises(Exception):  # noqa: B017,PT011 - coordinator raises on unhealthy start
        await service.start()
    assert service.status is ServiceStatus.FAILED
    assert stack.boundary.state is BoundaryState.STOPPED  # M2 unwound, no leak
    assert provider.disconnect_calls >= 1  # partially-connected provider disconnected
    assert service._observer_task is None and service._watch_task is None


# --------------------------------------------------------------------------- #
# Worker fault / no auto-restart / readiness fail-closed (F6 / §29 / §32)
# --------------------------------------------------------------------------- #
async def test_worker_fatal_exception_fails_closed_no_restart() -> None:
    publisher = _FakePublisher(transmit_error=RuntimeError("worker boom"))
    provider = _Provider([_tick()])
    stack = _stack(publisher)
    service = _service(stack, provider)
    await service.start()
    await asyncio.wait_for(service._watch_task, timeout=2.0)  # observer trips → fail closed

    assert service.status is ServiceStatus.FAILED
    assert service.terminal_failure is True
    assert provider.disconnect_calls >= 1
    assert stack.boundary.state is BoundaryState.FAILED  # no auto-restart of the worker
    assert stack.boundary.diagnostics().worker_running is False
    assert service.diagnostics().continuity_state == ContinuityState.BROKEN.value
    # A late event after the fault is rejected, never applied blind (readiness stays closed).
    with pytest.raises(PublicationTerminalError):
        stack.sink.handle(_tick())
    assert service.status is ServiceStatus.FAILED


# --------------------------------------------------------------------------- #
# Definite/unknown publication outcome never counts as success (F3 / F4 / §6)
# --------------------------------------------------------------------------- #
async def test_definite_publication_failure_is_terminal_never_success() -> None:
    publisher = _FakePublisher(transmit_outcome=PublishOutcome.FAILED_TRANSPORT)
    provider = _Provider([_tick()])
    stack = _stack(publisher)
    service = _service(stack, provider)
    await service.start()
    await asyncio.wait_for(service._watch_task, timeout=2.0)

    diagnostics = service.diagnostics()
    assert service.status is ServiceStatus.FAILED
    assert diagnostics.published_total == 0  # a failed transmit is never counted as published
    assert diagnostics.last_accepted_sequence == 1  # accepted leads published — no false success
    assert diagnostics.continuity_reason == ContinuityReason.PUBLICATION_FAILED.value


def test_unknown_outcome_capability_is_terminal_and_sticky() -> None:
    # D1's transmit contract can only return PUBLISHED | FAILED_TRANSPORT (the conflated case is
    # mapped to publication_failed); the distinct uncertain capability is terminal + sticky and
    # never claims loss or success — proven directly on L1.
    tracker = FeedContinuityTracker()
    tracker.producer_started(producer_id="p", producer_epoch=1)
    tracker.publication_uncertain()
    assert tracker.state is ContinuityState.BROKEN
    assert tracker.snapshot().reason is ContinuityReason.PUBLICATION_OUTCOME_UNCERTAIN
    tracker.publication_succeeded(producer_sequence=9)  # cannot clear a terminal break
    assert tracker.state is ContinuityState.BROKEN
    assert tracker.snapshot().reason is ContinuityReason.PUBLICATION_OUTCOME_UNCERTAIN


# --------------------------------------------------------------------------- #
# Overflow race — no overwrite/coalesce, terminal + sticky (F5 / F13)
# --------------------------------------------------------------------------- #
async def test_overflow_race_rejects_without_overwrite_and_is_terminal() -> None:
    gate = asyncio.Event()
    publisher = _FakePublisher(transmit_gate=gate)  # worker holds the first item in-flight
    stack = _stack(publisher, capacity=1)
    await stack.boundary.start()
    stack.continuity.producer_started(producer_id="market-ingestion", producer_epoch=1)

    stack.sink.handle(_tick("A"))  # enqueued (depth 1)
    await _until(lambda: publisher.transmit_calls >= 1)  # worker dequeued A, blocked in transmit
    stack.sink.handle(_tick("B"))  # enqueued (depth 1, capacity full)
    with pytest.raises(PublicationTerminalError):
        stack.sink.handle(_tick("C"))  # bounded queue full → explicit overflow (never silent)

    snapshot = stack.continuity.snapshot()
    assert stack.continuity.state is ContinuityState.BROKEN
    assert snapshot.reason is ContinuityReason.PUBLICATION_QUEUE_OVERFLOW
    assert snapshot.overflow_total == 1
    gate.set()  # release the worker so A and B (no coalescing) drain cleanly
    result = await stack.boundary.stop()
    assert result.pending_at_stop == 0  # A and B both published; only C was rejected
    assert stack.boundary.diagnostics().published_total == 2


# --------------------------------------------------------------------------- #
# Drain: clean vs incomplete (F11 / F12 / F13 / §20 / §21)
# --------------------------------------------------------------------------- #
async def test_clean_drain_publishes_everything_accepted() -> None:
    publisher = _FakePublisher()
    provider = _Provider([_tick("A"), _tick("B"), _tick("C")])
    stack = _stack(publisher)
    service = _service(stack, provider)
    await service.start()
    await service.wait()
    await service.stop()

    diagnostics = service.diagnostics()
    assert service.status is ServiceStatus.STOPPED
    assert service.terminal_failure is False
    assert diagnostics.published_total == 3
    assert diagnostics.last_accepted_sequence == 3  # accepted == published on a clean drain
    assert diagnostics.continuity_reason == ContinuityReason.CLEAN_SHUTDOWN.value


async def test_incomplete_drain_surfaces_pending_and_is_not_clean() -> None:
    gate = asyncio.Event()  # never released → the worker cannot drain
    publisher = _FakePublisher(transmit_gate=gate)
    stack = _stack(publisher, capacity=8, drain_timeout=0.0)
    await stack.boundary.start()
    stack.continuity.producer_started(producer_id="market-ingestion", producer_epoch=1)
    stack.sink.handle(_tick("A"))
    stack.sink.handle(_tick("B"))
    await _until(lambda: publisher.transmit_calls >= 1)  # worker holds A, B still queued

    result = await stack.boundary.stop()  # bounded drain times out
    stack.continuity.drain_completed(result)
    snapshot = stack.continuity.snapshot()
    assert result.drained_complete is False
    assert result.pending_at_stop == 2  # the in-flight A + queued B — never under-reported
    assert snapshot.state is ContinuityState.STOPPED
    assert snapshot.reason is ContinuityReason.INCOMPLETE_DRAIN


# --------------------------------------------------------------------------- #
# Terminal bypasses reconnect self-heal (F9 / F16 / §8 / §29)
# --------------------------------------------------------------------------- #
async def test_terminal_during_reconnect_cancels_the_reconnect() -> None:
    reconnect_sleep_gate = asyncio.Event()  # never released: the reconnect backoff is parked here

    async def _blocking_backoff(_seconds: float) -> None:
        await reconnect_sleep_gate.wait()

    publisher = _FakePublisher(transmit_error=RuntimeError("worker boom"))
    provider = _DropThenBlockProvider(_tick(), asyncio.Event())
    stack = _stack(publisher)
    service = _service(stack, provider, max_reconnects=5, supervisor_sleep=_blocking_backoff)
    await service.start()
    # Episode 1 drops (recoverable) → supervisor parks in backoff; the worker fault (terminal)
    # must win: fail-closed cancels the parked supervisor, so no reconnect episode ever starts.
    await asyncio.wait_for(service._watch_task, timeout=2.0)

    assert service.status is ServiceStatus.FAILED
    assert service.terminal_failure is True
    assert provider.stream_calls == 1  # reconnect episode never began
    assert provider.disconnect_calls >= 1


async def test_terminal_break_bypasses_supervisor_backoff() -> None:  # F9
    slept: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        slept.append(seconds)
        await asyncio.sleep(0)

    publisher = _FakePublisher()
    provider = _Provider([_tick("A"), _tick("B")])  # capacity 1 → B overflows → terminal
    stack = _stack(publisher, capacity=1)
    service = _service(stack, provider, max_reconnects=5, supervisor_sleep=_record_sleep)
    await service.start()
    await asyncio.wait_for(service._watch_task, timeout=2.0)

    assert service.status is ServiceStatus.FAILED
    assert service.terminal_failure is True
    assert slept == []  # overflow terminal propagated; the supervisor never slept/reconnected
    assert provider.stream_calls == 1


# --------------------------------------------------------------------------- #
# Shutdown races + idempotency (F14 / F18 / §17 / §18)
# --------------------------------------------------------------------------- #
async def test_stop_after_terminal_is_idempotent() -> None:
    publisher = _FakePublisher(transmit_error=RuntimeError("worker boom"))
    provider = _Provider([_tick()])
    stack = _stack(publisher)
    service = _service(stack, provider)
    await service.start()
    await asyncio.wait_for(service._watch_task, timeout=2.0)  # terminal fail-closed settled

    await service.stop()  # stop after a terminal must not raise or double-disconnect
    await service.stop()
    assert service.status is ServiceStatus.STOPPED
    assert service.terminal_failure is True  # the terminal history is preserved through stop
    assert provider.disconnect_calls == 1  # coordinator shutdown is single/idempotent


async def test_repeated_stop_is_idempotent() -> None:
    publisher = _FakePublisher()
    provider = _Provider([_tick()])
    stack = _stack(publisher)
    service = _service(stack, provider)
    await service.start()
    await service.wait()
    await service.stop()
    await service.stop()
    await service.stop()
    assert service.status is ServiceStatus.STOPPED
    assert provider.disconnect_calls == 1
    assert stack.boundary.state is BoundaryState.STOPPED


async def test_stop_racing_terminal_settles_without_unhandled_error() -> None:
    publisher = _FakePublisher(transmit_error=RuntimeError("worker boom"))
    provider = _Provider([_tick()])
    stack = _stack(publisher)
    service = _service(stack, provider)
    await service.start()
    # Do NOT wait for fail-closed: race stop() against the observer-driven terminal watcher.
    await service.stop()
    with contextlib.suppress(asyncio.CancelledError):
        if service._watch_task is not None:
            await service._watch_task
    assert service.status is ServiceStatus.STOPPED
    assert provider.disconnect_calls >= 1  # provider ends disconnected regardless of the race


# --------------------------------------------------------------------------- #
# Cancellation (F10 / §19)
# --------------------------------------------------------------------------- #
async def test_wait_tolerates_supervisor_cancellation() -> None:
    block = asyncio.Event()
    publisher = _FakePublisher()
    provider = _BlockingProvider(_tick(), block)
    stack = _stack(publisher)
    service = _service(stack, provider, max_reconnects=None)
    await service.start()
    waiter = asyncio.ensure_future(service.wait())
    await _until(lambda: publisher.transmit_calls >= 1)  # streaming underway

    service._supervisor_task.cancel()  # type: ignore[union-attr]
    await asyncio.wait_for(waiter, timeout=2.0)  # wait() returns; cancellation is tolerated
    assert waiter.exception() is None
    await service.stop()
    assert service.status is ServiceStatus.STOPPED


# --------------------------------------------------------------------------- #
# Lifecycle-boundary rejections (F22 / F23 / F24)
# --------------------------------------------------------------------------- #
async def test_sink_after_boundary_stopped_is_terminal_no_io() -> None:
    publisher = _FakePublisher()
    stack = _stack(publisher)
    await stack.boundary.start()
    stack.continuity.producer_started(producer_id="market-ingestion", producer_epoch=1)
    await stack.boundary.stop()

    before = publisher.transmit_calls
    with pytest.raises(PublicationTerminalError):
        stack.sink.handle(_tick())  # REJECTED_NOT_RUNNING → terminal, never enqueued
    assert publisher.transmit_calls == before  # no publication attempt after stop


async def test_prepare_rejection_is_non_terminal_service_continues() -> None:
    publisher = _FakePublisher(reject_symbols=frozenset({"BAD"}))
    provider = _Provider([_tick("BAD"), _tick("OK")])
    stack = _stack(publisher)
    service = _service(stack, provider)
    await service.start()
    await service.wait()
    await service.stop()

    snapshot = stack.continuity.snapshot()
    assert service.terminal_failure is False  # a malformed datum is not a continuity break
    assert snapshot.prepare_rejected_total == 1
    assert snapshot.state is ContinuityState.STOPPED
    assert stack.boundary.diagnostics().published_total == 1  # the valid event still published


# --------------------------------------------------------------------------- #
# terminal_failure property + entrypoint exit code (§32 / process-boundary fail-closed)
# --------------------------------------------------------------------------- #
async def test_terminal_failure_false_on_clean_supervisor_end() -> None:
    publisher = _FakePublisher()
    provider = _Provider([_tick()])
    service = _service(_stack(publisher), provider)
    await service.start()
    await service.wait()
    assert service.terminal_failure is False  # a clean stream end is not a terminal break
    await service.stop()
    assert service.terminal_failure is False


async def test_entrypoint_returns_nonzero_on_terminal_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.market_ingestion import __main__ as entry

    async def _compose(_settings: object) -> MarketIngestionService:
        publisher = _FakePublisher()
        provider = _Provider([_tick("A"), _tick("B")])  # capacity 1 → B overflows → terminal
        return _service(_stack(publisher, capacity=1), provider)

    monkeypatch.setattr(entry, "get_settings", lambda: object())
    monkeypatch.setattr(entry, "compose_market_ingestion_service", _compose)
    assert await entry._run() == 1  # a terminal publication break fails closed at the exit code


async def test_entrypoint_returns_zero_on_clean_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.market_ingestion import __main__ as entry

    async def _compose(_settings: object) -> MarketIngestionService:
        publisher = _FakePublisher()
        return _service(_stack(publisher), _Provider([_tick()]))

    monkeypatch.setattr(entry, "get_settings", lambda: object())
    monkeypatch.setattr(entry, "compose_market_ingestion_service", _compose)
    assert await entry._run() == 0  # clean serve-then-stop still exits zero
