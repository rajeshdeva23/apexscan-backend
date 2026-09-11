"""Broker-neutral market-data IPC contracts and transport abstractions (DECOUPLING PHASE A).

Reusable envelope, serialization, event-kind, producer-identity/dedup, universe-version,
compacted-reference, health, priority, config, and Redis Streams abstractions for the
future decoupled ingestion → Redis → backend path (DESIGN-REVIEW-2). Phase A is contracts
ONLY: nothing here is activated by application composition, the transport defaults OFF, and
the existing in-process ingestion path remains the sole authority.
"""

from __future__ import annotations

from app.market_ipc.config import MarketIpcConfig
from app.market_ipc.consumer import (
    ConsumerDiagnostics,
    MarketEventConsumer,
    MessageOutcome,
    RecordingShadowSink,
    ShadowMarketEventSink,
)
from app.market_ipc.dedup import BoundedDeduplicator
from app.market_ipc.envelope import (
    FEED_WIDE_IDENTITY,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    MarketEventEnvelope,
    ProducerEventIdentity,
    UniverseVersionComparison,
    build_envelope,
    compare_universe_version,
    decode_envelope,
    encode_envelope,
    identity_string_for,
    is_stale_trading_date,
)
from app.market_ipc.epoch import (
    LEGACY_REDIS_EPOCH_KEY_PREFIX,
    DurableEpochAllocator,
    EpochAllocator,
    EpochStateError,
)
from app.market_ipc.events import (
    EventKind,
    EventPriority,
    IpcPayload,
    decode_payload,
    encode_payload,
    event_kind_for,
    priority_for,
)
from app.market_ipc.publisher import (
    MarketEventPublisher,
    PublisherDiagnostics,
    PublishOutcome,
    StaticUniverseVersion,
    TradingDateSource,
    UniverseVersionSource,
)
from app.market_ipc.reference import (
    LoaderDiagnostics,
    RedisCompactedReferenceStore,
    ReferenceEntrySource,
    ReferenceOutcome,
    ReferenceSnapshot,
    ReferenceStateLoader,
    ReferenceStateWriter,
    WriterDiagnostics,
    merge_reference,
    reference_from_envelope,
)
from app.market_ipc.state import (
    CompactedReferenceState,
    CompactedReferenceStore,
    IngestionHealthState,
    InMemoryCompactedReferenceStore,
    health_key,
    reference_key,
)
from app.market_ipc.transport import (
    InMemoryMarketEventStream,
    MarketEventStream,
    RedisMarketEventStream,
    RedisPublishError,
)

__all__ = [
    "FEED_WIDE_IDENTITY",
    "LEGACY_REDIS_EPOCH_KEY_PREFIX",
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "BoundedDeduplicator",
    "CompactedReferenceState",
    "CompactedReferenceStore",
    "ConsumerDiagnostics",
    "DurableEpochAllocator",
    "EpochAllocator",
    "EpochStateError",
    "EventKind",
    "EventPriority",
    "IngestionHealthState",
    "InMemoryCompactedReferenceStore",
    "InMemoryMarketEventStream",
    "IpcPayload",
    "LoaderDiagnostics",
    "MarketEventConsumer",
    "MarketEventEnvelope",
    "MarketEventPublisher",
    "MarketEventStream",
    "MarketIpcConfig",
    "MessageOutcome",
    "ProducerEventIdentity",
    "PublishOutcome",
    "PublisherDiagnostics",
    "RecordingShadowSink",
    "RedisCompactedReferenceStore",
    "RedisMarketEventStream",
    "RedisPublishError",
    "ReferenceEntrySource",
    "ReferenceOutcome",
    "ReferenceSnapshot",
    "ReferenceStateLoader",
    "ReferenceStateWriter",
    "ShadowMarketEventSink",
    "StaticUniverseVersion",
    "WriterDiagnostics",
    "TradingDateSource",
    "UniverseVersionComparison",
    "UniverseVersionSource",
    "build_envelope",
    "compare_universe_version",
    "decode_envelope",
    "decode_payload",
    "encode_envelope",
    "encode_payload",
    "event_kind_for",
    "health_key",
    "identity_string_for",
    "is_stale_trading_date",
    "merge_reference",
    "priority_for",
    "reference_from_envelope",
    "reference_key",
]
