"""Phase-H market-path activation flags, illegal-combination validation, and mode derivation.

This is the single source of truth for the ADR-025 flag matrix. It is a **pure** module: it
imports nothing from the provider, Redis, M1/D1/M2/C1/L1, or application composition, and it
performs no I/O. :class:`Settings` holds the raw flags and delegates validation/derivation here so
one rule set governs both configuration-time validation and any future service composition.

H1 scope: the flags exist and validate; nothing here activates any path. Default flags derive
:attr:`MarketPathMode.LEGACY_ONLY` — the current production behaviour unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MarketPathMode(StrEnum):
    """The market-data authority/rollout mode derived from the activation flags (ADR-025)."""

    LEGACY_ONLY = "legacy_only"
    INGESTION_SHADOW_PUBLISH = "ingestion_shadow_publish"
    SHADOW_CONSUME_COMPARE = "shadow_consume_compare"
    IPC_AUTHORITATIVE_BACKEND = "ipc_authoritative_backend"
    # Terminal post-H10 code state (backend Dhan removed). Not derivable from flags alone — it is
    # a code-removal state, listed for completeness; H1 never returns it.
    LEGACY_PROVIDER_REMOVED = "legacy_provider_removed"


@dataclass(frozen=True, slots=True)
class PhaseHFlags:
    """The six independent Phase-H activation flags (ADR-025 flag matrix)."""

    market_ingestion_service_enabled: bool
    ipc_publisher_enabled: bool
    ipc_consumer_enabled: bool
    ipc_shadow_compare_enabled: bool
    ipc_authoritative_enabled: bool
    legacy_market_path_enabled: bool


class PhaseHConfigError(ValueError):
    """An illegal Phase-H activation flag combination (ADR-025)."""


def validate_phase_h_flags(flags: PhaseHFlags) -> None:
    """Reject every ADR-025 illegal flag combination; raise :class:`PhaseHConfigError` if invalid.

    The checks are fail-fast configuration guards, not runtime gates. Each message names the
    offending relationship (never a secret).
    """
    if flags.ipc_authoritative_enabled and not flags.ipc_consumer_enabled:
        raise PhaseHConfigError(
            "IPC_AUTHORITATIVE_ENABLED=true requires IPC_CONSUMER_ENABLED=true "
            "(authority has no source without a consumer)"
        )
    if flags.ipc_authoritative_enabled and flags.legacy_market_path_enabled:
        raise PhaseHConfigError(
            "IPC_AUTHORITATIVE_ENABLED=true is mutually exclusive with "
            "LEGACY_MARKET_PATH_ENABLED=true (dual authority into the TickEngine is forbidden)"
        )
    if flags.ipc_shadow_compare_enabled and not flags.ipc_consumer_enabled:
        raise PhaseHConfigError(
            "IPC_SHADOW_COMPARE_ENABLED=true requires IPC_CONSUMER_ENABLED=true "
            "(nothing to compare without a consumer)"
        )
    if flags.ipc_publisher_enabled and not flags.market_ingestion_service_enabled:
        raise PhaseHConfigError(
            "IPC_PUBLISHER_ENABLED=true requires MARKET_INGESTION_SERVICE_ENABLED=true "
            "(a publisher without its producer service)"
        )
    if not flags.legacy_market_path_enabled and not flags.ipc_authoritative_enabled:
        raise PhaseHConfigError(
            "LEGACY_MARKET_PATH_ENABLED=false requires IPC_AUTHORITATIVE_ENABLED=true "
            "(the backend would otherwise have no market authority)"
        )
    if (
        flags.ipc_consumer_enabled
        and not flags.ipc_shadow_compare_enabled
        and not flags.ipc_authoritative_enabled
    ):
        raise PhaseHConfigError(
            "IPC_CONSUMER_ENABLED=true requires IPC_SHADOW_COMPARE_ENABLED=true or "
            "IPC_AUTHORITATIVE_ENABLED=true (a consumer with no role would drain the stream "
            "and record dedup keys, poisoning dedup for a later authoritative switch)"
        )


def derive_market_path_mode(flags: PhaseHFlags) -> MarketPathMode:
    """Derive the rollout mode from validated flags; call :func:`validate_phase_h_flags` first.

    Assumes the flags are already legal (illegal combinations are rejected at configuration time),
    so the derivation is an unambiguous precedence over the surviving legal shapes.
    """
    validate_phase_h_flags(flags)
    if flags.ipc_authoritative_enabled:
        return MarketPathMode.IPC_AUTHORITATIVE_BACKEND
    if flags.ipc_shadow_compare_enabled:
        return MarketPathMode.SHADOW_CONSUME_COMPARE
    if flags.ipc_publisher_enabled:
        return MarketPathMode.INGESTION_SHADOW_PUBLISH
    return MarketPathMode.LEGACY_ONLY
