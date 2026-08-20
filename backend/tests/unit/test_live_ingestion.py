"""Managed live-market ingestion & feed-continuity wiring (RUN-C; ADR-010 D8/D9/D11).

Proves the single sequential ingestion task, manager-before-ingestion ordering, clean
cancellation, fatal-condition observation (failure/exhaustion), continuity reaching the
same TickEngine, and that authority stays disabled. Uses broker-neutral fake live
adapters — no network, no provider credentials.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.config import Settings
from app.events.bus import Event, EventBus
from app.market_engine.clock import ManualClock
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.sequence import MonotonicSequence
from app.schemas.market_data import (
    FeedContinuity,
    FeedContinuityEvent,
    Instrument,
    MarketData,
    SubscriptionRequest,
    Tick,
)
from app.services.market_runtime import LiveMarketRuntime, RuntimeState

_T0 = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)
_CLOCK = _T0 + timedelta(minutes=1)
_DB = "postgresql+asyncpg://user:pass@localhost:5432/apexscan"
_REDIS = "redis://localhost:6379/0"
_ERROR_THRESHOLD = 3


class _StreamError(RuntimeError):
    """A provider stream failure after the adapter's own reconnect is exhausted."""


def _settings() -> Settings:
    return Settings(app_env="development", database_url=_DB, redis_url=_REDIS)


def _instrument(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _tick(symbol: str, *, at: datetime = _T0) -> Tick:
    return Tick(instrument=_instrument(symbol), event_timestamp=at, last_price=Decimal("100"))


class _FakeLive:
    """A controllable broker-neutral live adapter (LiveMarketDataAdapter)."""

    def __init__(
        self,
        *,
        events: tuple[MarketData, ...] = (),
        continuity: tuple[FeedContinuityEvent, ...] = (),
        fail: bool = False,
        complete: bool = False,
    ) -> None:
        self._events = events
        self._continuity = continuity
        self._fail = fail
        self._complete = complete
        self._sink: Callable[[FeedContinuityEvent], None] | None = None
        self._gate = asyncio.Event()
        self.stream_calls = 0
        self.drained = asyncio.Event()

    def bind_continuity(self, sink: Callable[[FeedContinuityEvent], None]) -> None:
        self._sink = sink

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        self.stream_calls += 1
        for event in self._events:
            yield event
        for continuity in self._continuity:
            if self._sink is not None:
                self._sink(continuity)
        self.drained.set()
        if self._fail:
            raise _StreamError("live stream failed after reconnect exhausted")
        if self._complete:
            return
        await self._gate.wait()  # a live feed never completes normally — block until cancelled


def _runtime(*, instruments: tuple[Instrument, ...], live: _FakeLive | None) -> LiveMarketRuntime:
    return LiveMarketRuntime(
        settings=_settings(),
        error_threshold=_ERROR_THRESHOLD,
        instruments=instruments,
        live_market_data=live,
        clock=ManualClock(_CLOCK),
        sequence=MonotonicSequence(),
    )


def _recorder(bus: EventBus) -> list[Event]:
    recorded: list[Event] = []
    bus.subscribe(MarketContextCreated, recorded.append)
    bus.subscribe(MarketContextUpdated, recorded.append)
    return recorded


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not met in time")


# --------------------------------------------------------------------------- #
# Dormant (disabled) runtime
# --------------------------------------------------------------------------- #
async def test_disabled_runtime_creates_no_ingestion_task() -> None:
    runtime = _runtime(instruments=(), live=None)
    await runtime.start()
    status = runtime.status()
    assert status.ingestion_configured is False
    assert status.ingestion_running is False
    await runtime.shutdown()


# --------------------------------------------------------------------------- #
# Single sequential ingestion task
# --------------------------------------------------------------------------- #
async def test_start_creates_exactly_one_ingestion_task() -> None:
    live = _FakeLive()
    runtime = _runtime(instruments=(_instrument("RELIANCE"),), live=live)
    await runtime.start()
    await _wait_until(lambda: live.stream_calls == 1)
    assert runtime.status().ingestion_running is True
    await runtime.shutdown()


async def test_repeated_start_does_not_create_a_second_task() -> None:
    live = _FakeLive()
    runtime = _runtime(instruments=(_instrument("RELIANCE"),), live=live)
    await runtime.start()
    await _wait_until(lambda: live.stream_calls == 1)
    await runtime.start()  # idempotent
    await asyncio.sleep(0)
    assert live.stream_calls == 1
    await runtime.shutdown()


async def test_subscription_uses_the_runtime_universe() -> None:
    captured: list[SubscriptionRequest] = []

    class _CapturingLive(_FakeLive):
        async def stream_market_data(
            self, request: SubscriptionRequest
        ) -> AsyncIterator[MarketData]:
            captured.append(request)
            async for event in super().stream_market_data(request):
                yield event  # pragma: no cover

    universe = (_instrument("RELIANCE"), _instrument("TCS"))
    live = _CapturingLive()
    runtime = _runtime(instruments=universe, live=live)
    await runtime.start()
    await _wait_until(lambda: len(captured) == 1)
    assert captured[0].instruments == universe  # same canonical universe as the registry
    await runtime.shutdown()


# --------------------------------------------------------------------------- #
# Sequential dispatch through the SAME engine/registry/bus
# --------------------------------------------------------------------------- #
async def test_data_flows_through_the_same_engine_registry_and_bus() -> None:
    instrument = _instrument("RELIANCE")
    live = _FakeLive(events=(_tick("RELIANCE"),))
    runtime = _runtime(instruments=(instrument,), live=live)
    recorded = _recorder(runtime.bus)
    await runtime.start()
    await live.drained.wait()
    await _wait_until(lambda: len(recorded) == 1)
    assert [type(e) for e in recorded] == [MarketContextCreated]  # runtime.bus published it
    state = runtime.registry.get(instrument)
    assert state is not None and state.context is not None  # runtime.registry reflects it
    await runtime.shutdown()


async def test_cross_instrument_stream_order_is_preserved() -> None:
    universe = (_instrument("RELIANCE"), _instrument("TCS"))
    events = (
        _tick("RELIANCE", at=_T0),
        _tick("TCS", at=_T0),
        _tick("RELIANCE", at=_T0 + timedelta(seconds=1)),
    )
    live = _FakeLive(events=events)
    runtime = _runtime(instruments=universe, live=live)
    recorded = _recorder(runtime.bus)
    await runtime.start()
    await live.drained.wait()
    await _wait_until(lambda: len(recorded) == 3)
    kinds = [(type(e).__name__, e.context.instrument.symbol) for e in recorded]  # type: ignore[attr-defined]
    assert kinds == [
        ("MarketContextCreated", "RELIANCE"),
        ("MarketContextCreated", "TCS"),
        ("MarketContextUpdated", "RELIANCE"),
    ]
    await runtime.shutdown()


# --------------------------------------------------------------------------- #
# Cancellation & shutdown ordering
# --------------------------------------------------------------------------- #
async def test_shutdown_cancels_and_awaits_ingestion() -> None:
    live = _FakeLive()
    runtime = _runtime(instruments=(_instrument("RELIANCE"),), live=live)
    await runtime.start()
    await _wait_until(lambda: runtime.status().ingestion_running)
    await runtime.shutdown()
    status = runtime.status()
    assert status.state is RuntimeState.SHUTDOWN
    assert status.ingestion_running is False
    assert status.fatal_ingestion_error is False  # cancellation is not a fault


async def test_cancellation_is_not_a_fatal_condition() -> None:
    live = _FakeLive()
    runtime = _runtime(instruments=(_instrument("RELIANCE"),), live=live)
    await runtime.start()
    await _wait_until(lambda: runtime.status().ingestion_running)
    await runtime.shutdown()
    await asyncio.sleep(0)
    assert runtime.status().fatal_ingestion_error is False


async def test_shutdown_order_stops_ingestion_before_manager_unsubscribe() -> None:
    live = _FakeLive()
    runtime = _runtime(instruments=(_instrument("RELIANCE"),), live=live)
    await runtime.start()
    await _wait_until(lambda: runtime.status().ingestion_running)
    await runtime.shutdown()
    # After shutdown, ingestion has stopped and the manager is unsubscribed.
    assert runtime.status().ingestion_running is False
    assert runtime.manager_subscribed is False


# --------------------------------------------------------------------------- #
# Fatal conditions: stream failure / exhaustion
# --------------------------------------------------------------------------- #
async def test_stream_failure_is_recorded_and_not_restarted() -> None:
    instrument = _instrument("RELIANCE")
    live = _FakeLive(events=(_tick("RELIANCE"),), fail=True)
    runtime = _runtime(instruments=(instrument,), live=live)
    recorded = _recorder(runtime.bus)
    await runtime.start()
    await _wait_until(lambda: runtime.status().fatal_ingestion_error)
    assert len(recorded) == 1  # datum A was processed before the failure
    assert live.stream_calls == 1  # no auto-restart of the stream
    assert runtime.status().ingestion_running is False
    await runtime.shutdown()  # still safe


async def test_stream_exhaustion_is_treated_as_fatal() -> None:
    live = _FakeLive(complete=True)
    runtime = _runtime(instruments=(_instrument("RELIANCE"),), live=live)
    await runtime.start()
    await _wait_until(lambda: runtime.status().fatal_ingestion_error)
    assert runtime.status().ingestion_running is False
    await runtime.shutdown()


# --------------------------------------------------------------------------- #
# Feed continuity → same TickEngine
# --------------------------------------------------------------------------- #
async def test_continuity_reaches_the_runtime_tick_engine() -> None:
    instrument = _instrument("RELIANCE")
    events = (
        FeedContinuityEvent(status=FeedContinuity.DISCONNECTED, observed_at=_T0),
        FeedContinuityEvent(status=FeedContinuity.RECONNECTED, observed_at=_T0),
        FeedContinuityEvent(status=FeedContinuity.CONTINUITY_LOST, observed_at=_T0),
    )
    live = _FakeLive(continuity=events)
    runtime = _runtime(instruments=(instrument,), live=live)
    received: list[FeedContinuityEvent] = []

    def sink(event: FeedContinuityEvent) -> None:
        received.append(event)
        runtime.tick_engine.on_feed_continuity(event)  # the SAME engine the runtime owns

    live.bind_continuity(sink)
    await runtime.start()
    await live.drained.wait()
    await _wait_until(lambda: len(received) == 3)
    assert [event.status for event in received] == [
        FeedContinuity.DISCONNECTED,
        FeedContinuity.RECONNECTED,
        FeedContinuity.CONTINUITY_LOST,
    ]
    await runtime.shutdown()


# --------------------------------------------------------------------------- #
# Authority & invariants unchanged
# --------------------------------------------------------------------------- #
async def test_ingestion_does_not_enable_authority() -> None:
    live = _FakeLive(
        events=(_tick("RELIANCE"),),
        continuity=(FeedContinuityEvent(status=FeedContinuity.RECONNECTED, observed_at=_T0),),
    )
    instrument = _instrument("RELIANCE")
    runtime = _runtime(instruments=(instrument,), live=live)
    runtime_sink = runtime.tick_engine.on_feed_continuity
    live.bind_continuity(runtime_sink)
    await runtime.start()
    await live.drained.wait()
    status = runtime.status()
    assert status.staged_observation_verified is False
    assert status.tick_aggregate_verified is False
    await runtime.shutdown()


async def test_zero_strategies_and_timeframes_hold_under_ingestion() -> None:
    live = _FakeLive(events=(_tick("RELIANCE"),))
    instrument = _instrument("RELIANCE")
    runtime = _runtime(instruments=(instrument,), live=live)
    await runtime.start()
    await live.drained.wait()
    await _wait_until(lambda: runtime.registry.get(instrument) is not None)
    assert runtime.strategy_manager.evaluations_for(instrument) == ()  # no strategies
    assert runtime.candle_engine.timeframes == ()  # no timeframes
    assert runtime.fact_requirements.is_active() is False  # no session-statistics demand
    await runtime.shutdown()


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
async def test_replay_is_deterministic_for_the_same_stream() -> None:
    async def run() -> list[tuple[str, str]]:
        universe = (_instrument("RELIANCE"), _instrument("TCS"))
        events = (_tick("RELIANCE", at=_T0), _tick("TCS", at=_T0))
        live = _FakeLive(events=events)
        runtime = _runtime(instruments=universe, live=live)
        recorded = _recorder(runtime.bus)
        await runtime.start()
        await live.drained.wait()
        await _wait_until(lambda: len(recorded) == 2)
        result = [(type(e).__name__, e.context.instrument.symbol) for e in recorded]  # type: ignore[attr-defined]
        await runtime.shutdown()
        return result

    assert await run() == await run()
