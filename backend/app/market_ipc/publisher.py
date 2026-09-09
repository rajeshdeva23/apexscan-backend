"""Non-authoritative shadow publisher for canonical MarketData (DECOUPLING PHASE B).

Takes already-normalized broker-neutral :class:`MarketData` and publishes a versioned
:class:`MarketEventEnvelope` onto the Phase-A :class:`MarketEventStream` (Redis ``md:events``).
This is a SIDE path: it is off by default, non-authoritative, and its failures are isolated —
``publish`` never raises, so it can never disrupt the in-process TickEngine/EventBus pipeline.

Scope (Phase B): publisher capability + producer identity/epoch/sequence + ordering + payload
bounds + failure isolation. It owns only IPC publishing — no decoding, no TickEngine, no Redis
consumer, no compacted-reference/universe-resolver/historical/readiness work.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from app.market_ipc.config import MarketIpcConfig
from app.market_ipc.envelope import build_envelope, encode_envelope
from app.market_ipc.epoch import EpochAllocator
from app.market_ipc.events import IpcPayload
from app.market_ipc.transport import MarketEventStream, RedisPublishError
from app.schemas.market_data import (
    FeedContinuityEvent,
    MarketData,
    MarketReference,
    Quote,
    Tick,
)

# FeedContinuityEvent is broker-neutral but not part of the MarketData union; the publisher
# accepts both so it can shadow-publish continuity alongside per-instrument events.
PublishableEvent = MarketData | FeedContinuityEvent


class PublishOutcome(StrEnum):
    """The deterministic result of one publish attempt (never raised)."""

    PUBLISHED = "published"
    FAILED_UNSUPPORTED = "failed_unsupported"
    FAILED_OVERSIZE = "failed_oversize"
    FAILED_SERIALIZATION = "failed_serialization"
    FAILED_TRANSPORT = "failed_transport"


@runtime_checkable
class TradingDateSource(Protocol):
    """Supplies the authoritative exchange trading date for envelope stamping."""

    def current_trading_date(self) -> date:
        """Return the current authoritative trading date."""
        ...


@runtime_checkable
class UniverseVersionSource(Protocol):
    """Supplies the current universe version for envelope stamping."""

    def current_universe_version(self) -> int:
        """Return the current universe version."""
        ...


class StaticUniverseVersion:
    """Provisional Phase-B universe-version source (a fixed, configured value).

    The frozen architecture's versioned ``UniverseSnapshot``/``UniverseResolver`` is Phase E.
    Until then the publisher stamps a deterministic, configured version so the envelope contract
    is honoured without pretending dynamic reconciliation exists. The publisher reads it per
    event, so a future resolver can vary it without changing the publisher.
    """

    def __init__(self, version: int) -> None:
        if version < 0:
            raise ValueError("universe_version must be non-negative")
        self._version = version

    def current_universe_version(self) -> int:
        """Return the configured provisional universe version."""
        return self._version


class PublisherDiagnostics(BaseModel):
    """Bounded, credential-free publisher counters (no per-instrument cardinality)."""

    model_config = ConfigDict(frozen=True)

    running: bool
    producer_id: str
    producer_epoch: int | None
    current_sequence: int
    events_attempted_total: int
    events_published_total: int
    publish_failures_total: int
    serialization_failures_total: int
    oversize_rejections_total: int
    unsupported_type_total: int
    last_publish_at: datetime | None


class MarketEventPublisher:
    """Publishes canonical MarketData to the IPC stream; off-by-default, failure-isolated."""

    def __init__(
        self,
        *,
        stream: MarketEventStream,
        config: MarketIpcConfig,
        producer_id: str,
        epoch_allocator: EpochAllocator,
        trading_date_source: TradingDateSource,
        universe_version_source: UniverseVersionSource,
        now: Callable[[], datetime],
    ) -> None:
        if not producer_id:
            raise ValueError("producer_id must be non-empty")
        self._stream = stream
        self._config = config
        self._producer_id = producer_id
        self._epoch_allocator = epoch_allocator
        self._trading_date_source = trading_date_source
        self._universe_version_source = universe_version_source
        self._now = now
        self._epoch: int | None = None
        self._sequence = 0  # first published sequence within an epoch is 1
        self._counters = _Counters()
        self._last_publish_at: datetime | None = None

    @property
    def is_running(self) -> bool:
        """Whether the publisher has allocated an epoch and is ready to publish."""
        return self._epoch is not None

    async def start(self) -> None:
        """Allocate a restart-unique epoch and ensure the stream group exists.

        Raises whatever the allocator/stream raise (e.g. Redis down) so the caller can degrade
        safely — a publisher that cannot allocate an epoch must not run, and must not fabricate
        or reuse one.
        """
        if self._epoch is not None:
            return
        epoch = await self._epoch_allocator.allocate(self._producer_id)
        await self._stream.ensure_group()
        self._epoch = epoch
        self._sequence = 0

    async def publish(self, datum: PublishableEvent) -> PublishOutcome:
        """Publish one canonical event as a shadow copy; never raises.

        Each attempt consumes exactly one sequence number (advance-on-failure): a failed publish
        does not reuse its sequence for the next, different payload, so a ``(producer_id, epoch,
        sequence)`` identity is never bound to two payloads. No retry in Phase B.
        """
        if self._epoch is None:
            raise RuntimeError("publisher.publish called before start()")
        self._counters.attempted += 1
        try:
            payload: IpcPayload = _as_ipc_payload(datum)
        except ValueError:
            self._counters.unsupported += 1
            return PublishOutcome.FAILED_UNSUPPORTED

        self._sequence += 1
        try:
            envelope = build_envelope(
                payload,
                producer_id=self._producer_id,
                producer_epoch=self._epoch,
                producer_sequence=self._sequence,
                produced_at=self._now(),
                trading_date=self._trading_date_source.current_trading_date(),
                universe_version=self._universe_version_source.current_universe_version(),
            )
            encode_envelope(envelope, max_bytes=self._config.max_payload_bytes)
        except ValueError:
            self._counters.oversize += 1
            return PublishOutcome.FAILED_OVERSIZE
        except Exception:  # noqa: BLE001 - serialization must not escape the shadow path
            self._counters.serialization += 1
            return PublishOutcome.FAILED_SERIALIZATION

        try:
            await self._stream.publish(envelope)
        except RedisPublishError:
            self._counters.publish_failures += 1
            return PublishOutcome.FAILED_TRANSPORT
        self._counters.published += 1
        self._last_publish_at = self._now()
        return PublishOutcome.PUBLISHED

    def diagnostics(self) -> PublisherDiagnostics:
        """Snapshot the bounded publisher counters."""
        return PublisherDiagnostics(
            running=self.is_running,
            producer_id=self._producer_id,
            producer_epoch=self._epoch,
            current_sequence=self._sequence,
            events_attempted_total=self._counters.attempted,
            events_published_total=self._counters.published,
            publish_failures_total=self._counters.publish_failures,
            serialization_failures_total=self._counters.serialization,
            oversize_rejections_total=self._counters.oversize,
            unsupported_type_total=self._counters.unsupported,
            last_publish_at=self._last_publish_at,
        )


class _Counters:
    """Mutable bounded counters (fixed set of fields; no unbounded growth)."""

    __slots__ = (
        "attempted",
        "published",
        "publish_failures",
        "serialization",
        "oversize",
        "unsupported",
    )

    def __init__(self) -> None:
        self.attempted = 0
        self.published = 0
        self.publish_failures = 0
        self.serialization = 0
        self.oversize = 0
        self.unsupported = 0


def _as_ipc_payload(datum: PublishableEvent) -> IpcPayload:
    """Narrow a canonical datum to a Phase-A IPC payload, rejecting unsupported kinds."""
    if isinstance(datum, (Tick, Quote, MarketReference, FeedContinuityEvent)):
        return datum
    raise ValueError(f"unsupported IPC payload type: {type(datum).__name__}")
