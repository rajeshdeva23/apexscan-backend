"""Provider stream supervisor for the market-ingestion service (DECOUPLING PHASE H2).

Owns the live-subscription receive loop for one broker-neutral provider: it iterates
``stream_market_data`` and routes each decoded canonical event to a sink, self-healing a terminal
stream failure with bounded backoff (the same shape as the backend's in-process supervisor, but
provider-only — no TickEngine, no Redis, no IPC). The adapter owns within-stream reconnect; this
loop sits above it and restarts the stream. Cancellation propagates cleanly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum

from app.adapters.base.broker_adapter import LiveMarketDataAdapter
from app.market_ingestion.sink import ProviderOnlyEventSink
from app.schemas.market_data import SubscriptionRequest

logger = logging.getLogger(__name__)

_INITIAL_BACKOFF_SECONDS = 1.0
_MAXIMUM_BACKOFF_SECONDS = 30.0


class SupervisorStatus(StrEnum):
    """Provider-stream supervisor lifecycle status."""

    IDLE = "idle"
    STREAMING = "streaming"
    RECOVERING = "recovering"
    FAILED = "failed"  # reconnect budget exhausted (bounded runs only)


class ProviderSupervisor:
    """Iterate the provider live stream into a sink; self-heal terminal failures (bounded)."""

    def __init__(
        self,
        *,
        provider: LiveMarketDataAdapter,
        request: SubscriptionRequest,
        sink: ProviderOnlyEventSink,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        max_reconnects: int | None = None,
    ) -> None:
        """Wire the supervisor; ``max_reconnects=None`` self-heals indefinitely (production)."""
        self._provider = provider
        self._request = request
        self._sink = sink
        self._sleep = sleep or asyncio.sleep
        self._max_reconnects = max_reconnects
        self._status = SupervisorStatus.IDLE
        self._reconnects = 0
        self._consecutive_failures = 0
        self._last_failure: str | None = None

    @property
    def status(self) -> SupervisorStatus:
        """Current supervisor status."""
        return self._status

    @property
    def reconnect_total(self) -> int:
        """Total stream reconnect attempts."""
        return self._reconnects

    @property
    def last_failure(self) -> str | None:
        """Sanitized type name of the last terminal stream failure, if any."""
        return self._last_failure

    async def run(self) -> None:
        """Consume the stream, restarting on terminal failure until cancelled/budget exhausted."""
        while True:
            await self._consume_once()
            if self._max_reconnects is not None and self._reconnects >= self._max_reconnects:
                self._status = SupervisorStatus.FAILED
                return
            self._reconnects += 1
            self._status = SupervisorStatus.RECOVERING
            await self._sleep(self._backoff())

    async def _consume_once(self) -> None:
        """Run one stream attempt; record a terminal end (only cancellation propagates)."""
        try:
            self._status = SupervisorStatus.STREAMING
            async for datum in self._provider.stream_market_data(self._request):
                self._sink.handle(datum)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - a terminal stream fault self-heals above
            self._last_failure = type(error).__name__
            self._consecutive_failures += 1
            logger.warning("provider stream ended (%s); scheduling reconnect", self._last_failure)
            return
        # A live stream returning is itself abnormal; treat as a recoverable terminal end.
        self._last_failure = "stream_returned"
        self._consecutive_failures += 1

    def _backoff(self) -> float:
        """Bounded exponential backoff for the current consecutive-failure count."""
        exponent = max(0, self._consecutive_failures - 1)
        return float(min(_MAXIMUM_BACKOFF_SECONDS, _INITIAL_BACKOFF_SECONDS * (2**exponent)))
