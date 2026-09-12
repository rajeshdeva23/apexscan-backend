"""Market-ingestion service package (DECOUPLING PHASE H1/H2).

Models ``apexscan-market-ingestion`` as a distinct service: the Phase-H flag matrix (H1) plus a
real provider lifecycle (H2 — Dhan auth/provider/WebSocket ownership) with IPC publication still
OFF. Importing this package performs no I/O and starts nothing; the provider and its dependencies
are injected/constructed lazily by the composition root (see :mod:`app.market_ingestion.__main__`).
"""

from __future__ import annotations

from app.market_ingestion.mode import (
    MarketPathMode,
    PhaseHConfigError,
    PhaseHFlags,
    derive_market_path_mode,
    validate_phase_h_flags,
)
from app.market_ingestion.service import (
    MarketIngestionConfigurationError,
    MarketIngestionService,
    ServiceDiagnostics,
    ServiceStatus,
)
from app.market_ingestion.sink import ProviderOnlyEventSink, ProviderSinkDiagnostics
from app.market_ingestion.supervisor import ProviderSupervisor, SupervisorStatus

__all__ = [
    "MarketIngestionConfigurationError",
    "MarketIngestionService",
    "MarketPathMode",
    "PhaseHConfigError",
    "PhaseHFlags",
    "ProviderOnlyEventSink",
    "ProviderSinkDiagnostics",
    "ProviderSupervisor",
    "ServiceDiagnostics",
    "ServiceStatus",
    "SupervisorStatus",
    "derive_market_path_mode",
    "validate_phase_h_flags",
]
