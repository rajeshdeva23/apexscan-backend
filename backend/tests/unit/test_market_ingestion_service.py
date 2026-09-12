"""Unit tests for the market-ingestion service lifecycle (DECOUPLING PHASE H2).

Proves: disabled service is fully inert (H1 preserved); an enabled service boots the provider
lifecycle with fakes (connect → health → stream → sink) and reaches RUNNING; reconnect keeps the
same provider/auth owner; a failed start records FAILED and cleans up; shutdown is deterministic;
and no IPC / M1 / M2 / D1 / Redis / TickEngine work ever occurs. Importing the package is pure.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.market_ingestion.mode import MarketPathMode, PhaseHFlags
from app.market_ingestion.service import (
    MarketIngestionConfigurationError,
    MarketIngestionService,
    ServiceStatus,
)
from app.schemas.market_data import (
    Instrument,
    MarketData,
    MarketDataKind,
    ProviderHealth,
    ProviderStatus,
    SubscriptionRequest,
    Tick,
)


def _flags(*, ingestion: bool) -> PhaseHFlags:
    # H2 shape: ingestion service may run with the publisher OFF, so the market path is still
    # LEGACY_ONLY (legacy stays enabled). Single-Dhan-owner is enforced at the Settings level
    # (market_provider_enabled vs market_ingestion_service_enabled), not in these flags.
    return PhaseHFlags(
        market_ingestion_service_enabled=ingestion,
        ipc_publisher_enabled=False,
        ipc_consumer_enabled=False,
        ipc_shadow_compare_enabled=False,
        ipc_authoritative_enabled=False,
        legacy_market_path_enabled=True,
    )


def _tick() -> Tick:
    return Tick(
        instrument=Instrument(exchange="NSE", symbol="TCS"),
        event_timestamp=datetime(2026, 9, 13, 4, 0, tzinfo=UTC),
        last_price=Decimal("100.5"),
    )


def _request() -> SubscriptionRequest:
    return SubscriptionRequest(
        instruments=(Instrument(exchange="NSE", symbol="TCS"),),
        data_types=frozenset({MarketDataKind.TICK}),
    )


async def _noop_sleep(_seconds: float) -> None:
    return None


class _FakeProvider:
    """BrokerAdapter + LiveMarketDataAdapter fake with connect/health/stream fault injection."""

    def __init__(
        self, *, healthy: bool = True, episodes: list[tuple[list[MarketData], bool]] | None = None
    ) -> None:
        # Each episode = (events to yield, raise_at_end). Default: one episode, ends by raising.
        self._episodes = episodes if episodes is not None else [([_tick(), _tick()], True)]
        self._episode = 0
        self._healthy = healthy
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1  # idempotent in the real adapter; counted here for assertions

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def get_health(self) -> ProviderHealth:
        status = ProviderStatus.HEALTHY if self._healthy else ProviderStatus.UNAVAILABLE
        return ProviderHealth(status=status, observed_at=datetime(2026, 9, 13, 4, 0, tzinfo=UTC))

    async def stream_market_data(self, request: SubscriptionRequest) -> AsyncIterator[MarketData]:
        if self._episode >= len(self._episodes):
            return
        events, raise_at_end = self._episodes[self._episode]
        self._episode += 1
        for event in events:
            yield event
        if raise_at_end:
            raise ConnectionError("simulated provider disconnect")


# --------------------------------------------------------------------------- #
# Disabled / inert (H1 preserved)
# --------------------------------------------------------------------------- #
def test_disabled_service_is_inert() -> None:
    service = MarketIngestionService(flags=_flags(ingestion=False))
    assert service.enabled is False
    assert service.status == ServiceStatus.DISABLED
    assert service.mode is MarketPathMode.LEGACY_ONLY


async def test_disabled_start_is_a_noop() -> None:
    service = MarketIngestionService(flags=_flags(ingestion=False))
    await service.start()
    assert service.status == ServiceStatus.DISABLED


async def test_disabled_service_touches_no_dhan_epoch_or_redis() -> None:
    with (
        patch("app.adapters.dhan.auth.DhanAuthManager.get_access_token") as auth,
        patch("app.market_ipc.epoch.DurableEpochAllocator.allocate") as epoch,
        patch("redis.asyncio.Redis.from_url") as redis_from_url,
    ):
        service = MarketIngestionService(flags=_flags(ingestion=False))
        await service.start()
    assert auth.call_count == epoch.call_count == redis_from_url.call_count == 0


# --------------------------------------------------------------------------- #
# Enabled provider lifecycle (fakes)
# --------------------------------------------------------------------------- #
async def test_enabled_boot_reaches_running_and_feeds_sink() -> None:
    provider = _FakeProvider(episodes=[([_tick(), _tick()], False)])  # yields then ends
    service = MarketIngestionService(
        flags=_flags(ingestion=True),
        provider=provider,
        subscription_request=_request(),
        supervisor_max_reconnects=0,  # stop after the first stream ends
        supervisor_sleep=_noop_sleep,
    )
    await service.start()
    assert service.status is ServiceStatus.RUNNING
    assert provider.connect_calls >= 1
    await service.wait()  # supervisor ends (max_reconnects=0)
    assert service.diagnostics().events_total == 2
    await service.stop()
    assert service.status is ServiceStatus.STOPPED
    assert provider.disconnect_calls >= 1


async def test_enabled_start_without_provider_raises() -> None:
    service = MarketIngestionService(flags=_flags(ingestion=True))  # no provider/request
    with pytest.raises(MarketIngestionConfigurationError):
        await service.start()


async def test_enabled_boot_does_no_ipc_m1_m2_or_redis() -> None:
    provider = _FakeProvider(episodes=[([_tick()], False)])
    with (
        patch("app.market_ipc.epoch.DurableEpochAllocator.allocate") as epoch,
        patch("app.market_ipc.boundary.AsyncPublicationBoundary.start") as m2_start,
        patch("app.market_ipc.publisher.MarketEventPublisher.transmit") as d1_transmit,
        patch("redis.asyncio.Redis.from_url") as redis_from_url,
    ):
        service = MarketIngestionService(
            flags=_flags(ingestion=True),
            provider=provider,
            subscription_request=_request(),
            supervisor_max_reconnects=0,
            supervisor_sleep=_noop_sleep,
        )
        await service.start()
        await service.wait()
        await service.stop()
    assert epoch.call_count == 0  # M1 not allocated (publisher OFF)
    assert m2_start.call_count == 0  # M2 not activated
    assert d1_transmit.call_count == 0  # D1 not publishing
    assert redis_from_url.call_count == 0  # no Redis


async def test_reconnect_keeps_same_provider_owner() -> None:
    provider = _FakeProvider(
        episodes=[([_tick()], True), ([_tick()], True)]  # first stream raises → reconnect
    )
    service = MarketIngestionService(
        flags=_flags(ingestion=True),
        provider=provider,
        subscription_request=_request(),
        supervisor_max_reconnects=1,  # allow one reconnect then FAIL out for determinism
        supervisor_sleep=_noop_sleep,
    )
    await service.start()
    await service.wait()
    diagnostics = service.diagnostics()
    assert diagnostics.reconnect_total == 1
    assert service.provider is provider  # same provider/auth owner across reconnect
    assert provider.connect_calls == 1  # reconnect re-iterates the stream, never re-connects auth
    await service.stop()


async def test_failed_start_on_unhealthy_provider_cleans_up() -> None:
    provider = _FakeProvider(healthy=False)
    service = MarketIngestionService(
        flags=_flags(ingestion=True),
        provider=provider,
        subscription_request=_request(),
        supervisor_sleep=_noop_sleep,
    )
    with pytest.raises(Exception):  # noqa: B017 - coordinator raises a ProviderInitializationError
        await service.start()
    assert service.status is ServiceStatus.FAILED  # never falsely RUNNING
    assert provider.disconnect_calls >= 1  # cleaned up
    assert service.diagnostics().provider_connected is False


async def test_shutdown_is_deterministic_and_idempotent() -> None:
    provider = _FakeProvider(episodes=[([_tick()], False)])
    service = MarketIngestionService(
        flags=_flags(ingestion=True),
        provider=provider,
        subscription_request=_request(),
        supervisor_max_reconnects=None,  # would run forever; stop() must cancel it
        supervisor_sleep=_noop_sleep,
    )
    await service.start()
    await service.stop()
    await service.stop()  # idempotent
    assert service.status is ServiceStatus.STOPPED
    assert provider.disconnect_calls >= 1


# --------------------------------------------------------------------------- #
# Entrypoint + import purity
# --------------------------------------------------------------------------- #
async def test_entrypoint_run_disabled_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.market_ingestion import __main__ as entry

    async def _compose(_settings: object) -> MarketIngestionService:
        return MarketIngestionService(flags=_flags(ingestion=False))

    monkeypatch.setattr(entry, "get_settings", lambda: object())
    monkeypatch.setattr(entry, "compose_market_ingestion_service", _compose)
    assert await entry._run() == 0


async def test_entrypoint_run_enabled_serves_then_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.market_ingestion import __main__ as entry

    async def _compose(_settings: object) -> MarketIngestionService:
        return MarketIngestionService(
            flags=_flags(ingestion=True),
            provider=_FakeProvider(episodes=[([_tick()], False)]),
            subscription_request=_request(),
            supervisor_max_reconnects=0,  # supervisor ends → wait() returns → clean exit
            supervisor_sleep=_noop_sleep,
        )

    monkeypatch.setattr(entry, "get_settings", lambda: object())
    monkeypatch.setattr(entry, "compose_market_ingestion_service", _compose)
    assert await entry._run() == 0  # started, served, stopped cleanly — never connected real Dhan


def test_importing_package_has_no_live_side_effects() -> None:
    code = (
        "import sys\n"
        "import app.market_ingestion\n"
        "bad = [m for m in sys.modules if 'adapters.dhan' in m]\n"
        "assert not bad, bad\n"
        "assert 'app.market_ipc.epoch' not in sys.modules\n"
        "print('pure')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "pure" in result.stdout
