"""Unit proofs for the H4A consumer runtime composition (DECOUPLING PHASE H4A).

No Redis: the runtime lifecycle and composition logic are exercised over the in-memory stream and
a Redis double. Covers config-role validation (only SHADOW_CONSUME_COMPARE composes a live
runtime; illegal flag shapes fail closed), the inert/disabled runtime, the owned client being
closed exactly once with no leaked task, import purity, and independence from live timestamp
correctness (FIX-2). The real-Redis stream/durable-dedup behaviour lives in the integration suite.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.market_ingestion.mode import MarketPathMode, PhaseHConfigError, PhaseHFlags
from app.market_ipc import (
    InMemoryMarketEventStream,
    MarketEventConsumer,
    MarketEventConsumerRuntime,
    MarketIpcConfig,
    RecordingShadowSink,
    RuntimeState,
    build_envelope,
    compose_consumer_runtime,
)
from app.market_ipc import consumer_runtime as runtime_module
from app.schemas.market_data import Instrument, Tick

_NOW = datetime(2026, 9, 9, 10, 15, 30, tzinfo=UTC)
_TD = date(2026, 9, 9)


class _Settings:
    def __init__(self, flags: PhaseHFlags) -> None:
        self.redis_url = "unix:///nonexistent/h4a-unit.sock"  # never connected in a unit test
        self._flags = flags

    def phase_h_flags(self) -> PhaseHFlags:
        return self._flags

    def market_ipc_config(self) -> MarketIpcConfig:
        return MarketIpcConfig(block_ms=0)


def _flags(**overrides: bool) -> PhaseHFlags:
    base = {
        "market_ingestion_service_enabled": False,
        "ipc_publisher_enabled": False,
        "ipc_consumer_enabled": False,
        "ipc_shadow_compare_enabled": False,
        "ipc_authoritative_enabled": False,
        "legacy_market_path_enabled": True,
    }
    base.update(overrides)
    return PhaseHFlags(**base)


def _shadow_flags() -> PhaseHFlags:
    return _flags(ipc_consumer_enabled=True, ipc_shadow_compare_enabled=True)


class _CountingRedis:
    """Redis double that only counts how many times it is closed (no I/O)."""

    def __init__(self) -> None:
        self.closes = 0

    async def aclose(self) -> None:
        self.closes += 1


def _tick() -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol="TCS"),
        event_timestamp=_NOW,
        last_price=Decimal("100.5"),
    )


def _envelope(seq: int) -> object:
    return build_envelope(
        _tick(),
        producer_id="market-ingestion",
        producer_epoch=1,
        producer_sequence=seq,
        produced_at=_NOW,
        trading_date=_TD,
        universe_version=7,
    )


def _inmem_runtime(
    transport: InMemoryMarketEventStream,
    sink: RecordingShadowSink,
    *,
    redis: object | None = None,
) -> MarketEventConsumerRuntime:
    config = MarketIpcConfig(block_ms=0)
    consumer = MarketEventConsumer(
        transport=transport,
        config=config,
        sink=sink,
        trading_date_source=lambda: _TD,
        universe_version_source=lambda: 7,
        now=lambda: _NOW,
    )
    return MarketEventConsumerRuntime(
        mode=MarketPathMode.SHADOW_CONSUME_COMPARE,
        flags=_shadow_flags(),
        consumer=consumer,
        redis=redis,
        poll_idle_seconds=0.01,
    )


# --------------------------------------------------------------------------- #
# T01 / T02: config-role validation
# --------------------------------------------------------------------------- #
async def test_compose_shadow_mode_builds_enabled_runtime() -> None:
    runtime = await compose_consumer_runtime(_Settings(_shadow_flags()))
    assert runtime.enabled
    assert runtime.mode is MarketPathMode.SHADOW_CONSUME_COMPARE
    assert runtime.state is RuntimeState.NOT_STARTED  # composed, not started (no Redis connect)
    assert not runtime.is_ready
    await runtime.stop()  # closes the never-connected client cleanly


async def test_compose_legacy_default_is_disabled_runtime() -> None:
    runtime = await compose_consumer_runtime(_Settings(_flags()))  # defaults -> LEGACY_ONLY
    assert not runtime.enabled
    assert runtime.mode is MarketPathMode.LEGACY_ONLY
    assert runtime.state is RuntimeState.DISABLED
    assert runtime.diagnostics() is None


async def test_compose_rejects_consumer_without_a_role() -> None:
    # A consumer with neither shadow-compare nor authority would drain the stream and poison
    # dedup: the ADR-025 matrix rejects it fail-closed (no runtime is built).
    with pytest.raises(PhaseHConfigError):
        await compose_consumer_runtime(_Settings(_flags(ipc_consumer_enabled=True)))


async def test_compose_rejects_authoritative_shape() -> None:
    with pytest.raises(PhaseHConfigError):
        await compose_consumer_runtime(
            _Settings(
                _flags(
                    ipc_consumer_enabled=True,
                    ipc_authoritative_enabled=True,
                    legacy_market_path_enabled=True,
                )
            )
        )


# --------------------------------------------------------------------------- #
# Disabled runtime lifecycle is fully inert
# --------------------------------------------------------------------------- #
async def test_disabled_runtime_start_stop_are_noops() -> None:
    runtime = MarketEventConsumerRuntime(mode=MarketPathMode.LEGACY_ONLY, flags=_flags())
    await runtime.start()
    assert runtime.state is RuntimeState.DISABLED
    assert not runtime.is_ready
    await runtime.stop()
    assert runtime.state is RuntimeState.DISABLED


# --------------------------------------------------------------------------- #
# T18 / T19: owned client closed exactly once, no leaked task
# --------------------------------------------------------------------------- #
async def test_stop_closes_client_once_and_clears_task() -> None:
    transport = InMemoryMarketEventStream()
    redis = _CountingRedis()
    runtime = _inmem_runtime(transport, RecordingShadowSink(), redis=redis)
    await runtime.start()
    assert runtime.is_ready
    task = runtime._task  # noqa: SLF001
    assert task is not None and not task.done()
    await runtime.stop()
    await runtime.stop()  # idempotent

    assert redis.closes == 1
    assert runtime._task is None  # noqa: SLF001
    assert task.done()  # no leaked task
    assert runtime.state is RuntimeState.STOPPED


# --------------------------------------------------------------------------- #
# T28: the apply path does not depend on live timestamp correctness (FIX-2)
# --------------------------------------------------------------------------- #
async def test_apply_path_is_independent_of_live_timestamps() -> None:
    transport = InMemoryMarketEventStream()
    for seq in range(1, 4):
        await transport.publish(_envelope(seq))
    sink = RecordingShadowSink()
    runtime = _inmem_runtime(transport, sink)
    await runtime.start()
    try:
        for _ in range(100):
            if sink.applied_total == 3:
                break
            await asyncio.sleep(0.01)
    finally:
        await runtime.stop()

    assert sink.applied_total == 3  # deterministic fixture timestamps only; no live-clock coupling


# --------------------------------------------------------------------------- #
# T40: import purity
# --------------------------------------------------------------------------- #
def test_import_is_side_effect_free() -> None:
    assert inspect.iscoroutinefunction(compose_consumer_runtime)
    disabled = MarketEventConsumerRuntime(mode=MarketPathMode.LEGACY_ONLY, flags=_flags())
    assert disabled._redis is None  # no client created for the inert shape  # noqa: SLF001
    # The module exposes composition, not a running consumer: no module-level Redis/task/provider.
    assert not hasattr(runtime_module, "redis_lifecycle")
