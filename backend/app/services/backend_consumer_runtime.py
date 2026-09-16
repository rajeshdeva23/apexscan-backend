"""Backend-root composition for the decoupled IPC consumer runtime (DECOUPLING PHASE H9B, §17).

Wires :func:`app.market_ipc.consumer_runtime.compose_consumer_runtime` into the actual backend
application root with ONE shared timezone-aware UTC clock driving both the consumer's ``now`` (the
event-age / redelivery-horizon gate) and its ``trading_date_source`` (via the same session
classifier the producer uses). There is no second time abstraction, and Dhan LTT is never used as
wall-clock ``now`` (FIX-2 is untouched).

Inert by default: under the default LEGACY_ONLY flag shape ``compose_consumer_runtime`` returns a
disabled runtime that owns no Redis client and runs no task, so composing/starting/stopping it in
the lifespan changes nothing until the shadow/authority flags are explicitly enabled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.market_engine.session import MarketSessionClassifier
from app.market_ingestion.publication import SessionTradingDate
from app.market_ipc.consumer_runtime import (
    MarketEventConsumerRuntime,
    compose_consumer_runtime,
)

if TYPE_CHECKING:
    from app.core.config import Settings


async def compose_backend_consumer_runtime(settings: Settings) -> MarketEventConsumerRuntime:
    """Compose the backend IPC consumer runtime over one shared aware-UTC clock (H9B §17)."""

    def _now() -> datetime:
        return datetime.now(UTC)

    classifier = MarketSessionClassifier.from_settings(settings)
    trading_date = SessionTradingDate(classify=classifier.classify, now=_now)
    return await compose_consumer_runtime(
        settings,
        trading_date_source=trading_date.current_trading_date,
        now=_now,
    )
