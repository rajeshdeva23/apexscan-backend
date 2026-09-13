"""Composition root for the market-ingestion service (DECOUPLING PHASE H2 / H3A).

Builds a real, provider-owning :class:`MarketIngestionService` from settings when the service is
enabled, and a disabled/inert service otherwise. Provider construction (and the Dhan adapter
import) is **lazy** — it happens only inside this async builder when the service is enabled, so
importing the package remains side-effect free. In publisher mode (H3A) it additionally builds the
M1/D1/M2/L1 publication stack over Redis; the M1 epoch is still allocated later by
``boundary.start()``. Reaching the publisher branch requires ``live_h3_publish_approved`` (the
Settings interlock refuses to construct otherwise), so an accidental live enable cannot occur.

Ownership: the ingestion service owns one Dhan auth manager / provider (one connect ⇒ one cached
token). ``connect`` is idempotent and token generation is lazy, so resolving the universe here and
re-connecting in :meth:`MarketIngestionService.start` does not regenerate the token.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.market_ingestion.service import MarketIngestionService

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.market_ingestion.publication import PublicationStack
    from app.schemas.market_data import Instrument

# Stable producer identity for the decoupled ingestion service (M1 producer_id).
_PRODUCER_ID = "market-ingestion"


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
    publication = _build_publication(settings) if flags.ipc_publisher_enabled else None
    return MarketIngestionService(
        flags=flags,
        provider=provider,
        subscription_request=request,
        publication=publication,
        provider_lifecycle_timeout_seconds=settings.provider_lifecycle_timeout_seconds,
    )


def _build_publication(settings: Settings) -> PublicationStack:
    """Construct the H3 publication stack over the configured Redis (publisher mode; live-gated).

    The trading date is resolved per event by :class:`SessionTradingDate` over the canonical
    :class:`MarketSessionClassifier`, so a long-lived producer crosses exchange-local session
    boundaries without a restart. The Redis client is created here and owned by the returned stack
    (closed on shutdown by the service).
    """
    from redis.asyncio import Redis

    from app.market_engine.session import MarketSessionClassifier
    from app.market_ingestion.publication import SessionTradingDate, build_publication_stack

    def _utc_now() -> datetime:
        return datetime.now(UTC)

    redis: Redis = Redis.from_url(settings.redis_url)
    classifier = MarketSessionClassifier.from_settings(settings)
    return build_publication_stack(
        redis=redis,
        config=settings.market_ipc_config(),
        producer_id=_PRODUCER_ID,
        state_dir=Path(settings.market_ingestion_state_dir),
        now=_utc_now,
        trading_date_source=SessionTradingDate(classify=classifier.classify, now=_utc_now),
    )


def _canonical_universe(instruments: Sequence[Instrument]) -> tuple[Instrument, ...]:
    """Fail-closed: require a non-empty, duplicate-free canonical universe (ADR-004)."""
    resolved = tuple(instruments)
    if not resolved:
        raise UniverseResolutionError("enabled provider resolved an empty cash-equity universe")
    if len(set(resolved)) != len(resolved):
        raise UniverseResolutionError("duplicate canonical instruments in the resolved universe")
    return resolved
