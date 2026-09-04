"""Session-aware self-healing of terminal live-ingestion failure (MARKET-INGESTION-RESILIENCE-1).

Reproduces the VIEW-1C-R3 production failure: a terminal ``ProviderUnavailableError`` (adapter
reconnect exhausted) left ingestion permanently dead. These deterministic tests prove the
supervisor now recovers it — bounded, session-aware, single-owner, and without rebuilding the
provider (so its cached token is reused). No network, no credentials.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from app.adapters.base.errors import ProviderUnavailableError
from app.core.config import Settings
from app.events.bus import Event, EventBus
from app.market_engine.clock import ManualClock
from app.market_engine.events import MarketContextCreated, MarketContextUpdated
from app.market_engine.sequence import MonotonicSequence
from app.market_engine.session import MarketSessionClassifier, SessionSchedule, TradingCalendar
from app.schemas.market_data import Instrument, MarketData, SubscriptionRequest, Tick
from app.services.market_runtime import (
    IngestionRecoveryPolicy,
    IngestionState,
    LiveMarketRuntime,
    RuntimeState,
)

_DB = "postgresql+asyncpg://user:pass@localhost:5432/apexscan"
_REDIS = "redis://localhost:6379/0"
_LIVE = datetime(2026, 8, 6, 6, 30, tzinfo=UTC)  # 12:00 IST — LIVE_SESSION
_CLOSED = datetime(2026, 8, 6, 14, 30, tzinfo=UTC)  # 20:00 IST — MARKET_CLOSED
_SCHEDULE = SessionSchedule(
    pre_open_start=time(9, 0),
    opening_auction_start=time(9, 8),
    regular_open=time(9, 15),
    regular_close=time(15, 30),
    closing_end=time(15, 40),
)


def _settings() -> Settings:
    return Settings(app_env="development", database_url=_DB, redis_url=_REDIS)


def _instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _tick(symbol: str = "RELIANCE", *, offset: int = 0) -> Tick:
    # Timestamps sit just before the clock instant (never future -> never rejected) and strictly
    # increase with offset so a recovered stream's tick is accepted, not seen as a duplicate.
    return Tick(
        instrument=_instrument(symbol),
        event_timestamp=_LIVE - timedelta(seconds=10) + timedelta(seconds=offset),
        last_price=Decimal("100") + Decimal(offset),
        traded_quantity=1,
    )


def _classifier() -> MarketSessionClassifier:
    return MarketSessionClassifier(
        schedule=_SCHEDULE, calendar=TradingCalendar(holidays=()), exchange_timezone="Asia/Kolkata"
    )


class _ScriptedProvider:
    """A live adapter whose per-stream-call behavior is scripted; tracks single-ownership.

    Crucially it is a SINGLE instance reused across recoveries — recovery must re-consume this
    same object (proving the runtime never rebuilds the provider or re-authenticates).
    """

    def __init__(self, script: list[tuple[str, tuple[Tick, ...]]]) -> None:
        self._script = list(script)
        self.stream_calls = 0
        self.max_concurrent_streams = 0
        self._active = 0
        self._block = asyncio.Event()

    def bind_continuity(self, sink: Callable[[object], None]) -> None:
        """Match the adapter interface used by composition (unused here)."""

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        index = self.stream_calls
        self.stream_calls += 1
        self._active += 1
        self.max_concurrent_streams = max(self.max_concurrent_streams, self._active)
        try:
            kind, events = self._script[index] if index < len(self._script) else ("block", ())
            for event in events:
                yield event
            if kind == "fail":
                raise ProviderUnavailableError()
            await self._block.wait()  # "recovered" / healthy: a live feed never returns
        finally:
            self._active -= 1


def _runtime(
    provider: _ScriptedProvider,
    *,
    clock: ManualClock,
    sleep: Callable[[float], object],
    policy: IngestionRecoveryPolicy | None = None,
) -> LiveMarketRuntime:
    return LiveMarketRuntime(
        settings=_settings(),
        error_threshold=3,
        instruments=(_instrument("RELIANCE"),),
        live_market_data=provider,
        clock=clock,
        sequence=MonotonicSequence(),
        session_classifier=_classifier(),
        ingestion_sleep=sleep,  # type: ignore[arg-type]
        ingestion_random=lambda: 0.5,
        ingestion_recovery_policy=policy,
    )


def _recorder(bus: EventBus) -> list[Event]:
    recorded: list[Event] = []
    bus.subscribe(MarketContextCreated, recorded.append)
    bus.subscribe(MarketContextUpdated, recorded.append)
    return recorded


async def _yielding_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


async def _wait_until(predicate: Callable[[], bool], *, limit: int = 5000) -> None:
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not met in time")


# --------------------------------------------------------------------------- #
# Backoff policy (pure)
# --------------------------------------------------------------------------- #
def test_recovery_backoff_is_bounded_and_grows() -> None:
    policy = IngestionRecoveryPolicy(
        initial_delay_seconds=1.0, maximum_delay_seconds=8.0, jitter_ratio=0.0
    )
    delays = [policy.delay_for_failure(n, 0.5) for n in range(1, 8)]
    assert delays[0] == 1.0
    assert delays[1] == 2.0
    assert delays[2] == 4.0
    assert delays[3] == 8.0
    assert all(d <= 8.0 for d in delays)  # capped, never unbounded


# --------------------------------------------------------------------------- #
# Recovery during a live session
# --------------------------------------------------------------------------- #
async def test_provider_unavailable_during_live_session_recovers() -> None:
    provider = _ScriptedProvider([("fail", (_tick(offset=0),)), ("block", (_tick(offset=1),))])
    runtime = _runtime(provider, clock=ManualClock(_LIVE), sleep=_yielding_sleep)
    recorded = _recorder(runtime.bus)
    await runtime.start()
    await _wait_until(lambda: provider.stream_calls >= 2)  # failed run, then a fresh run
    await _wait_until(lambda: len(recorded) >= 2)  # event from the recovered stream reached the bus
    assert runtime.status().fatal_ingestion_error is False
    assert runtime.ingestion_diagnostics().recovery_attempts >= 1
    await runtime.shutdown()


async def test_recovery_uses_exactly_one_ingestion_owner() -> None:
    provider = _ScriptedProvider([("fail", ()), ("fail", ()), ("block", (_tick(),))])
    runtime = _runtime(provider, clock=ManualClock(_LIVE), sleep=_yielding_sleep)
    await runtime.start()
    await _wait_until(lambda: provider.stream_calls >= 3)
    assert provider.max_concurrent_streams == 1  # never two concurrent streams
    await runtime.shutdown()


async def test_recovery_reuses_the_same_provider_instance() -> None:
    # Token-generation safety: the runtime must NOT rebuild the provider on recovery, so the
    # adapter's cached access token is reused (no re-auth). Prove the same object is re-consumed.
    provider = _ScriptedProvider([("fail", ()), ("block", (_tick(),))])
    runtime = _runtime(provider, clock=ManualClock(_LIVE), sleep=_yielding_sleep)
    await runtime.start()
    await _wait_until(lambda: provider.stream_calls >= 2)
    assert runtime._live_market_data is provider  # same instance across recovery
    await runtime.shutdown()


async def test_consecutive_failures_grow_backoff_no_tight_loop() -> None:
    calls: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        calls.append(seconds)
        await asyncio.sleep(0)

    provider = _ScriptedProvider([("fail", ()), ("fail", ()), ("fail", ()), ("block", ())])
    policy = IngestionRecoveryPolicy(
        initial_delay_seconds=1.0, maximum_delay_seconds=8.0, jitter_ratio=0.0
    )
    runtime = _runtime(provider, clock=ManualClock(_LIVE), sleep=_record_sleep, policy=policy)
    await runtime.start()
    await _wait_until(lambda: provider.stream_calls >= 4)
    # Backoff grows across consecutive failures (no zero-delay tight loop).
    assert calls[:3] == [1.0, 2.0, 4.0]
    await runtime.shutdown()


# --------------------------------------------------------------------------- #
# Session-aware: closed market must not aggressively reconnect
# --------------------------------------------------------------------------- #
async def test_no_recovery_while_market_closed() -> None:
    provider = _ScriptedProvider([("fail", ())])  # first run fails; then market is closed
    runtime = _runtime(provider, clock=ManualClock(_CLOSED), sleep=_yielding_sleep)
    await runtime.start()
    await _wait_until(
        lambda: runtime.ingestion_diagnostics().state == IngestionState.WAITING_FOR_SESSION
    )
    # Give the loop many turns: it must NOT re-attempt the stream while closed.
    for _ in range(50):
        await asyncio.sleep(0)
    assert provider.stream_calls == 1  # no reconnect storm overnight
    assert runtime.status().fatal_ingestion_error is False
    await runtime.shutdown()


async def test_closed_then_live_session_recovers_without_restart() -> None:
    # THE CRITICAL TEST — reproduces R3: failure while closed, then recovery at the next
    # live session, all without a container/runtime restart.
    clock = ManualClock(_CLOSED)
    provider = _ScriptedProvider([("fail", ()), ("block", (_tick(),))])

    async def _flip_to_live_then_yield(_seconds: float) -> None:
        clock.set(_LIVE)  # a session that expects ingestion has begun
        await asyncio.sleep(0)

    runtime = _runtime(provider, clock=clock, sleep=_flip_to_live_then_yield)
    await runtime.start()
    await _wait_until(lambda: provider.stream_calls >= 2)  # resumed once live, no restart
    assert runtime.status().state is RuntimeState.STARTED  # never shut down / restarted
    assert runtime.status().fatal_ingestion_error is False
    await runtime.shutdown()


# --------------------------------------------------------------------------- #
# Shutdown during backoff
# --------------------------------------------------------------------------- #
async def test_shutdown_during_backoff_cancels_cleanly() -> None:
    held = asyncio.Event()

    async def _blocking_sleep(_seconds: float) -> None:
        held.set()
        await asyncio.Event().wait()  # stay in backoff until cancelled

    provider = _ScriptedProvider([("fail", ())])
    runtime = _runtime(provider, clock=ManualClock(_LIVE), sleep=_blocking_sleep)
    await runtime.start()
    await held.wait()  # supervisor is now parked in recovery backoff
    assert runtime.ingestion_diagnostics().state == IngestionState.RECOVERING
    await runtime.shutdown()  # must cancel cleanly, not hang
    assert runtime.status().state is RuntimeState.SHUTDOWN
    assert runtime.status().fatal_ingestion_error is False  # cancellation is not a fault


# --------------------------------------------------------------------------- #
# Health/observability signal + isolation
# --------------------------------------------------------------------------- #
async def test_runtime_does_not_report_running_while_recovering() -> None:
    held = asyncio.Event()

    async def _blocking_sleep(_seconds: float) -> None:
        held.set()
        await asyncio.Event().wait()

    provider = _ScriptedProvider([("fail", ())])
    runtime = _runtime(provider, clock=ManualClock(_LIVE), sleep=_blocking_sleep)
    await runtime.start()
    await held.wait()
    assert runtime.status().ingestion_running is False  # not falsely "running" during recovery
    assert runtime.ingestion_diagnostics().ingestion_task_running is False
    assert runtime.ingestion_diagnostics().recovery_task_running is True
    await runtime.shutdown()


async def test_recovery_has_no_strategy_or_authority_side_effects() -> None:
    provider = _ScriptedProvider([("fail", (_tick(),)), ("block", (_tick(),))])
    runtime = _runtime(provider, clock=ManualClock(_LIVE), sleep=_yielding_sleep)
    await runtime.start()
    await _wait_until(lambda: provider.stream_calls >= 2)
    status = runtime.status()
    assert status.staged_observation_verified is False  # authority never enabled by recovery
    assert status.tick_aggregate_verified is False
    assert runtime.candle_engine.timeframes == ()
    await runtime.shutdown()
