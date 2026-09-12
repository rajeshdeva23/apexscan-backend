"""Composition root for the market-ingestion service (DECOUPLING PHASE H2).

Builds a real, provider-owning :class:`MarketIngestionService` from settings when the service is
enabled, and a disabled/inert service otherwise. Provider construction (and the Dhan adapter
import) is **lazy** — it happens only inside this async builder when the service is enabled, so
importing the package remains side-effect free. IPC publication stays OFF: nothing here allocates
an M1 epoch, starts M2, constructs the D1 publisher, or touches Redis.

Ownership: the ingestion service owns one Dhan auth manager / provider (one connect ⇒ one cached
token). ``connect`` is idempotent and token generation is lazy, so resolving the universe here and
re-connecting in :meth:`MarketIngestionService.start` does not regenerate the token.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.market_ingestion.service import MarketIngestionService

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.schemas.market_data import Instrument


class UniverseResolutionError(RuntimeError):
    """Raised when the enabled provider resolves an empty or duplicated cash-equity universe."""


async def compose_market_ingestion_service(settings: Settings) -> MarketIngestionService:
    """Build the ingestion service; disabled → inert, enabled → provider-owning (IPC still OFF)."""
    flags = settings.phase_h_flags()
    if not flags.market_ingestion_service_enabled:
        return MarketIngestionService(flags=flags)

    # Lazy imports: only an enabled service pulls the Dhan provider surface.
    from app.adapters.dhan.adapter import DhanRestAdapter
    from app.schemas.market_data import MarketDataKind, SubscriptionRequest

    provider = DhanRestAdapter.from_settings(settings)
    try:
        await provider.connect()  # idempotent; creates HTTP clients, no token/WS yet
        await provider.load_instruments()
        universe = _canonical_universe(
            tuple(
                ref.instrument
                for ref in provider.load_nse_cash_equity_live_universe().cash_references
            )
        )
    except BaseException:
        # A universe-resolution/connect failure must not leak the opened provider connection.
        with contextlib.suppress(Exception):
            await provider.disconnect()
        raise
    request = SubscriptionRequest(instruments=universe, data_types=frozenset({MarketDataKind.TICK}))
    return MarketIngestionService(
        flags=flags,
        provider=provider,
        subscription_request=request,
        provider_lifecycle_timeout_seconds=settings.provider_lifecycle_timeout_seconds,
    )


def _canonical_universe(instruments: Sequence[Instrument]) -> tuple[Instrument, ...]:
    """Fail-closed: require a non-empty, duplicate-free canonical universe (ADR-004)."""
    resolved = tuple(instruments)
    if not resolved:
        raise UniverseResolutionError("enabled provider resolved an empty cash-equity universe")
    if len(set(resolved)) != len(resolved):
        raise UniverseResolutionError("duplicate canonical instruments in the resolved universe")
    return resolved
