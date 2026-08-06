"""Controlled, network-free provider adapter for lifecycle tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.adapters.base.broker_adapter import BrokerAdapter
from app.schemas.market_data import ProviderCapability, ProviderHealth, ProviderStatus

_OBSERVED_AT = datetime(2026, 8, 4, 9, 15, tzinfo=UTC)


class ControlledProviderAdapter(BrokerAdapter):
    """A test adapter with controllable health and lifecycle failures."""

    capabilities = frozenset[ProviderCapability]()

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.health_status = ProviderStatus.HEALTHY
        self.connect_error: Exception | None = None
        self.disconnect_error: Exception | None = None
        self.health_error: Exception | None = None
        self.block_connect = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.health_calls = 0
        self._connect_gate = asyncio.Event()

    async def connect(self) -> None:
        """Record a connection operation and apply configured test behavior."""
        self.events.append("provider.connect")
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        if self.block_connect:
            await self._connect_gate.wait()

    async def disconnect(self) -> None:
        """Record cleanup and apply configured test behavior."""
        self.events.append("provider.disconnect")
        self.disconnect_calls += 1
        if self.disconnect_error is not None:
            raise self.disconnect_error

    async def get_health(self) -> ProviderHealth:
        """Return configured canonical health or raise a configured failure."""
        self.events.append("provider.health")
        self.health_calls += 1
        if self.health_error is not None:
            raise self.health_error
        return ProviderHealth(status=self.health_status, observed_at=_OBSERVED_AT)
