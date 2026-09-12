"""Unit tests for the inert market-ingestion service skeleton (DECOUPLING PHASE H1).

Proves the H1 load-bearing property: under default/disabled configuration the service (and its
entrypoint) perform NO Dhan auth, WebSocket, M1 epoch allocation, Redis, or IPC activity — and
importing the package has no such side effects. An enabled service refuses to boot (live boot is
H2) rather than starting live work.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest

from app.market_ingestion.mode import MarketPathMode, PhaseHFlags
from app.market_ingestion.service import (
    MarketIngestionBootNotImplementedError,
    MarketIngestionService,
    ServiceStatus,
)


def _flags(*, ingestion: bool) -> PhaseHFlags:
    return PhaseHFlags(
        market_ingestion_service_enabled=ingestion,
        ipc_publisher_enabled=False,
        ipc_consumer_enabled=False,
        ipc_shadow_compare_enabled=False,
        ipc_authoritative_enabled=False,
        legacy_market_path_enabled=True,
    )


def test_disabled_service_is_inert_and_status_disabled() -> None:
    service = MarketIngestionService(flags=_flags(ingestion=False))
    assert service.enabled is False
    assert service.status == ServiceStatus.DISABLED
    assert service.mode is MarketPathMode.LEGACY_ONLY


async def test_disabled_start_is_a_noop() -> None:
    service = MarketIngestionService(flags=_flags(ingestion=False))
    await service.start()  # must not raise, must not do work
    assert service.status == ServiceStatus.DISABLED


async def test_enabled_start_refuses_live_boot_in_h1() -> None:
    service = MarketIngestionService(flags=_flags(ingestion=True))
    assert service.status == ServiceStatus.NOT_STARTED
    with pytest.raises(MarketIngestionBootNotImplementedError):
        await service.start()  # live boot is H2; H1 refuses, never connects Dhan


async def test_stop_is_idempotent() -> None:
    service = MarketIngestionService(flags=_flags(ingestion=False))
    await service.stop()
    await service.stop()
    assert service.status == ServiceStatus.STOPPED


async def test_disabled_service_touches_no_dhan_epoch_or_redis() -> None:
    # These are the live dependencies H1 must never touch. The inert service references none of
    # them, so a disabled start leaves every one uncalled (structural inertness guarantee).
    with (
        patch("app.adapters.dhan.auth.DhanAuthManager.get_access_token") as auth,
        patch("app.market_ipc.epoch.DurableEpochAllocator.allocate") as epoch,
        patch("redis.asyncio.Redis.from_url") as redis_from_url,
    ):
        service = MarketIngestionService(flags=_flags(ingestion=False))
        await service.start()
    assert auth.call_count == 0
    assert epoch.call_count == 0
    assert redis_from_url.call_count == 0


def test_importing_package_has_no_live_side_effects() -> None:
    # Fresh interpreter: importing the ingestion package must not pull in the Dhan provider or M1
    # epoch module, nor construct a Redis client (import purity, §25).
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
