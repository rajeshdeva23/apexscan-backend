"""Producer→backend ingestion-health conveyance over ``md:health`` (DECOUPLING PHASE H9B, B11).

The producer L1 continuity record (:class:`FeedContinuitySnapshot`) survives a Redis loss because
it lives on the ingestion host, but the *backend* process cannot see it directly. B11's loss
detector needs that producer evidence to attribute a downstream absence to the producer rather than
to a Redis loss. This module is the single conveyance:

    producer:  FeedContinuitySnapshot -> IngestionHealthState -> md:health (SET, TTL-bounded)
    backend:   md:health -> IngestionHealthState -> ProducerPublicationEvidence (staleness-gated)

There is exactly ONE health truth (``IngestionHealthState`` at ``md:health``). The reader fails
closed: a missing, malformed, or stale snapshot yields no evidence, so B11 cannot read a stale
producer position as fresh. Off by default — nothing writes/reads ``md:health`` until composed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError
from redis.exceptions import RedisError

from app.market_ipc.loss_detection import ProducerPublicationEvidence
from app.market_ipc.state import IngestionHealthState, health_key
from app.schemas.market_data import ProviderStatus

if TYPE_CHECKING:
    from datetime import datetime

    from redis.asyncio import Redis

    from app.market_ipc.config import MarketIpcConfig
    from app.market_ipc.continuity import FeedContinuitySnapshot


def ingestion_health_from_continuity(
    snapshot: FeedContinuitySnapshot,
    *,
    updated_at: datetime,
    market_data_age_seconds: float | None = None,
    last_event_at: datetime | None = None,
) -> IngestionHealthState:
    """Project the producer L1 continuity snapshot into the broker-neutral md:health model.

    The three B11 fields are set identically to :meth:`ProducerPublicationEvidence.from_continuity`
    so the round-trip through Redis reconstitutes the same evidence. ``universe_sync`` stays UNKNOWN
    here (no producer/consumer universe reconciliation is wired in H9B).
    """
    from app.market_ipc.continuity import ContinuityReason, ContinuityState

    if snapshot.producer_id is None or snapshot.producer_epoch is None:
        raise ValueError("cannot publish ingestion health before the producer incarnation started")
    healthy = snapshot.state is ContinuityState.HEALTHY
    return IngestionHealthState(
        producer_id=snapshot.producer_id,
        producer_epoch=snapshot.producer_epoch,
        updated_at=updated_at,
        ingestion=ProviderStatus.HEALTHY if healthy else ProviderStatus.DOWN,
        transport=ProviderStatus.HEALTHY if snapshot.provider_connected else ProviderStatus.DOWN,
        universe_sync=ProviderStatus.UNKNOWN,
        market_data_age_seconds=market_data_age_seconds,
        last_event_at=last_event_at,
        last_published_sequence=snapshot.last_published_sequence,
        terminal_publication_break=snapshot.state is ContinuityState.BROKEN,
        publication_outcome_uncertain=(
            snapshot.reason is ContinuityReason.PUBLICATION_OUTCOME_UNCERTAIN
        ),
    )


class IngestionHealthPublisher:
    """Producer-side writer: serialize an :class:`IngestionHealthState` to ``md:health`` (TTL)."""

    def __init__(self, redis: Redis, config: MarketIpcConfig) -> None:
        self._redis = redis
        self._config = config

    async def publish(self, state: IngestionHealthState) -> bool:
        """Write the snapshot with a TTL; return whether it committed (Redis error → ``False``)."""
        try:
            await self._redis.set(
                health_key(self._config),
                state.model_dump_json(),
                ex=self._config.health_ttl_seconds,
            )
        except RedisError:
            return False
        return True


class IngestionHealthReader:
    """Backend-side reader: decode ``md:health`` and gate it on freshness (fail closed)."""

    def __init__(self, redis: Redis, config: MarketIpcConfig) -> None:
        self._redis = redis
        self._config = config

    async def read(self) -> IngestionHealthState | None:
        """Return the decoded snapshot, or ``None`` when absent, unreadable, or malformed."""
        try:
            raw = await self._redis.get(health_key(self._config))
        except RedisError:
            return None
        if raw is None:
            return None
        try:
            return IngestionHealthState.model_validate_json(raw)
        except ValidationError:
            return None

    async def read_evidence(self, now: datetime) -> ProducerPublicationEvidence | None:
        """Producer evidence for B11, or ``None`` when the snapshot is missing/malformed/stale.

        Freshness is measured against ``updated_at``: a snapshot older than
        ``config.health_stale_seconds`` — or one dated implausibly far in the future — yields no
        evidence, so a stale producer position can never be read as current.
        """
        state = await self.read()
        if state is None:
            return None
        age_seconds = (now - state.updated_at).total_seconds()
        if abs(age_seconds) > self._config.health_stale_seconds:
            return None
        return ProducerPublicationEvidence.from_ingestion_health(state)
