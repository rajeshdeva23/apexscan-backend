"""Market-ingestion service package (DECOUPLING PHASE H1/H2/H3A).

Models ``apexscan-market-ingestion`` as a distinct service: the Phase-H flag matrix (H1), a real
provider lifecycle (H2 — Dhan auth/provider/WebSocket ownership), and shadow-publish publisher mode
(H3A — M1/D1/M2/L1 → Redis) with the IPC consumer/C1 still OFF and the backend legacy path
authoritative. Importing this package performs no I/O and starts nothing; the provider and the
heavy publication stack (:mod:`app.market_ingestion.publication`) are constructed lazily by the
composition root, so this import never pulls ``market_ipc`` transport code.
"""

from __future__ import annotations

from app.market_ingestion.errors import PublicationTerminalError
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
from app.market_ingestion.sink import EventSink, ProviderOnlyEventSink, ProviderSinkDiagnostics
from app.market_ingestion.supervisor import ProviderSupervisor, SupervisorStatus

__all__ = [
    "EventSink",
    "MarketIngestionConfigurationError",
    "MarketIngestionService",
    "MarketPathMode",
    "PhaseHConfigError",
    "PhaseHFlags",
    "ProviderOnlyEventSink",
    "ProviderSinkDiagnostics",
    "ProviderSupervisor",
    "PublicationTerminalError",
    "ServiceDiagnostics",
    "ServiceStatus",
    "SupervisorStatus",
    "derive_market_path_mode",
    "validate_phase_h_flags",
]
