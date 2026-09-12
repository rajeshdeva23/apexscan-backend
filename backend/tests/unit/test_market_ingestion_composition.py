"""Unit tests for the market-ingestion composition root (DECOUPLING PHASE H2).

Covers the enabled provider-owning build path with a fake Dhan adapter (no real network), the
connection-leak-free failure path (connect/universe error → provider disconnected + raised), and
the fail-closed universe guards — none of which the entrypoint tests exercise (they monkeypatch
compose out).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.market_ingestion import composition
from app.market_ingestion.composition import (
    UniverseResolutionError,
    compose_market_ingestion_service,
)
from app.market_ingestion.mode import PhaseHFlags
from app.market_ingestion.service import ServiceStatus
from app.schemas.market_data import Instrument


def _settings(*, ingestion: bool) -> SimpleNamespace:
    flags = PhaseHFlags(
        market_ingestion_service_enabled=ingestion,
        ipc_publisher_enabled=False,
        ipc_consumer_enabled=False,
        ipc_shadow_compare_enabled=False,
        ipc_authoritative_enabled=False,
        legacy_market_path_enabled=True,
    )
    return SimpleNamespace(phase_h_flags=lambda: flags, provider_lifecycle_timeout_seconds=30.0)


class _FakeUniverseProvider:
    """Fake Dhan adapter for composition: connect/load/universe/disconnect with fault injection."""

    def __init__(
        self, *, instruments: list[Instrument] | None = None, fail_load: bool = False
    ) -> None:
        self._instruments = (
            instruments if instruments is not None else [Instrument(exchange="NSE", symbol="TCS")]
        )
        self._fail_load = fail_load
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def load_instruments(self) -> None:
        if self._fail_load:
            raise TimeoutError("simulated instrument-master fetch timeout")

    def load_nse_cash_equity_live_universe(self) -> SimpleNamespace:
        return SimpleNamespace(
            cash_references=[SimpleNamespace(instrument=i) for i in self._instruments]
        )


def _patch_provider(monkeypatch: pytest.MonkeyPatch, provider: _FakeUniverseProvider) -> None:
    from app.adapters.dhan.adapter import DhanRestAdapter

    monkeypatch.setattr(DhanRestAdapter, "from_settings", classmethod(lambda cls, s: provider))


async def test_disabled_composition_returns_inert_service() -> None:
    service = await compose_market_ingestion_service(_settings(ingestion=False))  # type: ignore[arg-type]
    assert service.enabled is False
    assert service.status is ServiceStatus.DISABLED


async def test_enabled_composition_builds_provider_owning_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeUniverseProvider()
    _patch_provider(monkeypatch, provider)
    service = await compose_market_ingestion_service(_settings(ingestion=True))  # type: ignore[arg-type]
    assert service.enabled is True
    assert service.provider is provider
    assert provider.connect_calls == 1
    assert provider.disconnect_calls == 0  # success path does not disconnect


async def test_composition_failure_disconnects_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _FakeUniverseProvider(fail_load=True)
    _patch_provider(monkeypatch, provider)
    with pytest.raises(TimeoutError):
        await compose_market_ingestion_service(_settings(ingestion=True))  # type: ignore[arg-type]
    assert provider.disconnect_calls == 1  # no leaked connection on failure


async def test_composition_rejects_empty_universe(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _FakeUniverseProvider(instruments=[])
    _patch_provider(monkeypatch, provider)
    with pytest.raises(UniverseResolutionError):
        await compose_market_ingestion_service(_settings(ingestion=True))  # type: ignore[arg-type]
    assert provider.disconnect_calls == 1


async def test_composition_rejects_duplicate_universe(monkeypatch: pytest.MonkeyPatch) -> None:
    dup = Instrument(exchange="NSE", symbol="TCS")
    provider = _FakeUniverseProvider(instruments=[dup, dup])
    _patch_provider(monkeypatch, provider)
    with pytest.raises(UniverseResolutionError):
        await compose_market_ingestion_service(_settings(ingestion=True))  # type: ignore[arg-type]


def test_canonical_universe_helper_rejects_empty() -> None:
    with pytest.raises(UniverseResolutionError):
        composition._canonical_universe(())
