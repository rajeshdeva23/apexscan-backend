"""Inert market-ingestion service package (DECOUPLING PHASE H1).

Models ``apexscan-market-ingestion`` as a distinct service (composition + Phase-H flag matrix +
lifecycle skeleton) while remaining inert under all default configuration. Importing this package
performs no I/O and starts nothing (see :mod:`app.market_ingestion.service`). Live boot is H2.
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
    MarketIngestionBootNotImplementedError,
    MarketIngestionService,
    ServiceStatus,
)

__all__ = [
    "MarketIngestionBootNotImplementedError",
    "MarketIngestionService",
    "MarketPathMode",
    "PhaseHConfigError",
    "PhaseHFlags",
    "ServiceStatus",
    "derive_market_path_mode",
    "validate_phase_h_flags",
]
