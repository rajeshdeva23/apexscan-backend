"""Passive live Sector Intelligence shadow runtime (SECTOR-VIEW-1B).

A read-only EventBus observer plus a single periodic evaluator that reuses the SECTOR-2/3/4
pure engines to produce an internal ``SectorShadowSnapshot``. No provider I/O, no persistence,
no public API, no strategy or provider-lifecycle effects. Disabled by default
(``settings.sector_shadow_enabled``); see ADR-016, ADR-017, and docs/sector_view/.
"""

from app.services.sector_intelligence.config import SHADOW_VALIDATION_POLICY, ShadowRuntimeConfig
from app.services.sector_intelligence.diagnostics import ShadowDiagnosticsView
from app.services.sector_intelligence.runtime import SectorShadowRuntime
from app.services.sector_intelligence.snapshot import SCHEMA_VERSION, SectorShadowSnapshot
from app.services.sector_intelligence.state import (
    LatestObservation,
    ObservationState,
    RecordOutcome,
)

__all__ = [
    "SCHEMA_VERSION",
    "SHADOW_VALIDATION_POLICY",
    "LatestObservation",
    "ObservationState",
    "RecordOutcome",
    "SectorShadowRuntime",
    "SectorShadowSnapshot",
    "ShadowDiagnosticsView",
    "ShadowRuntimeConfig",
]
