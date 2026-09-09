"""Broker-neutral dynamic F&O universe: versioned UniverseSnapshot authority (DECOUPLING PHASE E).

The UniverseSnapshot is the shared, immutable, content-addressed authority for the effective live
F&O universe on a trading date. Ingestion (subscription view) and backend (expected-universe view)
are consumers of the SAME promoted snapshot. Governance: resolve automatically, promote explicitly
(fail-closed on unresolved mappings / empty universe). Nothing here is wired into production
composition, opens a WebSocket, contacts Dhan, or activates the IPC publisher/consumer.
"""

from __future__ import annotations

from app.market_universe.diagnostics import (
    UniverseDiagnostics,
    UniverseMetrics,
    build_universe_diagnostics,
)
from app.market_universe.resolver import (
    EmptyUniverseError,
    InMemoryProviderMappingSource,
    PromotionValidationError,
    ProviderMapping,
    ProviderMappingSource,
    ResolutionResult,
    SectorAuthority,
    UniverseResolver,
    UnresolvedInstrument,
    UnresolvedReason,
    validate_promotable,
)
from app.market_universe.snapshot import (
    CANDIDATE_VERSION,
    SCHEMA_VERSION,
    SnapshotState,
    SourceProvenance,
    UniverseDiff,
    UniverseInstrument,
    UniverseSnapshot,
    build_snapshot,
    content_sha256,
    diff_snapshots,
)
from app.market_universe.store import (
    FileUniverseSnapshotStore,
    SnapshotNotFoundError,
    UniverseSnapshotStore,
)
from app.market_universe.views import (
    BackendExpectedUniverse,
    SnapshotUniverseVersion,
    SubscriptionEntry,
    SubscriptionUniverse,
    next_trading_date,
    to_backend_universe,
    to_subscription_universe,
)

__all__ = [
    "CANDIDATE_VERSION",
    "SCHEMA_VERSION",
    "BackendExpectedUniverse",
    "EmptyUniverseError",
    "FileUniverseSnapshotStore",
    "InMemoryProviderMappingSource",
    "ProviderMapping",
    "ProviderMappingSource",
    "PromotionValidationError",
    "ResolutionResult",
    "SectorAuthority",
    "SnapshotNotFoundError",
    "SnapshotState",
    "SnapshotUniverseVersion",
    "SourceProvenance",
    "SubscriptionEntry",
    "SubscriptionUniverse",
    "UniverseDiagnostics",
    "UniverseDiff",
    "UniverseInstrument",
    "UniverseMetrics",
    "UniverseResolver",
    "UniverseSnapshot",
    "UniverseSnapshotStore",
    "UnresolvedInstrument",
    "UnresolvedReason",
    "build_snapshot",
    "build_universe_diagnostics",
    "content_sha256",
    "diff_snapshots",
    "next_trading_date",
    "to_backend_universe",
    "to_subscription_universe",
    "validate_promotable",
]
