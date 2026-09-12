"""Inert market-ingestion service composition skeleton (DECOUPLING PHASE H1).

Represents ``apexscan-market-ingestion`` as a distinct service so the repository can model the
two-service topology of ADR-025 — while remaining **completely inert** under all current/default
configuration. H1 adds only the lifecycle skeleton (create → validate → start → stop) and status;
it constructs no provider, allocates no M1 epoch, connects no Redis, and performs no Dhan auth /
WebSocket / publication. Live boot (still with the publisher OFF) is H2; nothing here begins it.

Construction and import are pure: importing this module or building the service does no I/O.
"""

from __future__ import annotations

import logging

from app.market_ingestion.mode import (
    MarketPathMode,
    PhaseHFlags,
    derive_market_path_mode,
    validate_phase_h_flags,
)

logger = logging.getLogger(__name__)


class ServiceStatus:
    """Inert service status values (H1). Deliberately excludes any READY state (ADR-025 §27)."""

    DISABLED = "disabled"
    NOT_STARTED = "not_started"
    STOPPED = "stopped"


class MarketIngestionBootNotImplementedError(RuntimeError):
    """Raised if an *enabled* ingestion service is started in H1 (live boot is H2).

    H1 must never connect Dhan / allocate an epoch / publish; if the service is enabled, startup
    refuses explicitly rather than silently doing nothing or beginning live activity.
    """


class MarketIngestionService:
    """Lifecycle skeleton for the future market-ingestion service — inert in H1.

    Validates the Phase-H flag matrix at construction (fail-fast). When the service is disabled
    (the default), :meth:`start` is a no-op recording the inert status. When enabled, :meth:`start`
    refuses (live boot is H2) — it never performs Dhan / M1 / Redis / publication work in H1.
    """

    def __init__(self, *, flags: PhaseHFlags) -> None:
        """Store validated flags only; construct no live dependency (lazy, no I/O).

        The bounded IPC transport config is surfaced by ``Settings.market_ipc_config()`` and is
        not needed by the H1 skeleton (nothing publishes yet); it is wired in H2.
        """
        validate_phase_h_flags(flags)  # fail-fast on an illegal combination
        self._flags = flags
        self._status = (
            ServiceStatus.NOT_STARTED
            if flags.market_ingestion_service_enabled
            else ServiceStatus.DISABLED
        )

    @property
    def status(self) -> str:
        """Current inert lifecycle status (never READY in H1)."""
        return self._status

    @property
    def mode(self) -> MarketPathMode:
        """The rollout mode derived from the configured flags."""
        return derive_market_path_mode(self._flags)

    @property
    def enabled(self) -> bool:
        """Whether the ingestion service is enabled (still inert in H1)."""
        return self._flags.market_ingestion_service_enabled

    async def start(self) -> None:
        """Start the service — inert when disabled; explicitly refuse live boot when enabled (H1).

        Raises:
            MarketIngestionBootNotImplementedError: If the service is enabled (live boot is H2).
        """
        if not self._flags.market_ingestion_service_enabled:
            self._status = ServiceStatus.DISABLED
            logger.info("market-ingestion service disabled; inert (no Dhan/IPC/Redis activity)")
            return
        raise MarketIngestionBootNotImplementedError(
            "market-ingestion live boot is Phase H2; H1 refuses to start an enabled service "
            "(no Dhan auth, WebSocket, epoch allocation, or IPC publication is performed)"
        )

    async def stop(self) -> None:
        """Stop the service (idempotent); no live resources exist to release in H1."""
        self._status = ServiceStatus.STOPPED
