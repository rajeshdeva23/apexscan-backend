"""Read-only market-authority diagnostics projection (DECOUPLING PHASE H9C-P4, Gate K).

Projects the already-read operational signals (ownership lease, token-mint metadata, ``md:health``,
and the B11 authority-readiness evaluator) into one bounded, machine-readable, **secret-free**
response. It is a pure function over values the endpoint has already fetched read-only — it performs
no Redis/Dhan I/O, no mutation, and never acquires ownership or mints a token. It exposes only
identifiers and states (owner role, uuid instance id, fencing generation, producer id/epoch,
sequences, health/readiness enums) — never a token, PIN, TOTP, or Redis/DB credential.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from app.market_ingestion.ownership import OwnershipSnapshot
    from app.market_ipc.loss_detection import LossDetectionResult
    from app.market_ipc.state import IngestionHealthState


class OwnershipView(BaseModel):
    """Who currently holds the single-Dhan-owner lease (read-only view; ADR-030)."""

    model_config = ConfigDict(frozen=True)

    has_owner: bool
    owner_role: str | None
    instance_id: str | None
    fencing_generation: int | None


class TokenMintView(BaseModel):
    """The last recorded token-mint reservation (metadata only; never the token)."""

    model_config = ConfigDict(frozen=True)

    recorded: bool
    last_mint_at_ms: int | None
    owner_role: str | None
    instance_id: str | None
    fencing_generation: int | None


class IngestionHealthView(BaseModel):
    """The producer's ``md:health`` snapshot as the backend currently sees it."""

    model_config = ConfigDict(frozen=True)

    present: bool
    stale: bool
    producer_id: str | None
    producer_epoch: int | None
    ingestion: str | None
    transport: str | None
    last_published_sequence: int | None
    terminal_publication_break: bool | None
    publication_outcome_uncertain: bool | None
    updated_at: datetime | None


class AuthorityReadinessView(BaseModel):
    """The B11 authority-readiness verdict (fail-closed; never hides insufficiency behind true)."""

    model_config = ConfigDict(frozen=True)

    ready: bool
    state: str
    reason: str


class ConsumerProgressView(BaseModel):
    """Backend consumer progress relative to the producer stream."""

    model_config = ConfigDict(frozen=True)

    last_applied_epoch: int | None
    last_applied_sequence: int | None
    stream_length: int | None
    pending: int | None


class MarketAuthorityDiagnostics(BaseModel):
    """One bounded, secret-free operator view of market-provider authority state (Gate K)."""

    model_config = ConfigDict(frozen=True)

    build_sha: str
    ownership_enabled: bool
    ownership: OwnershipView
    token_mint: TokenMintView
    ingestion_health: IngestionHealthView
    authority: AuthorityReadinessView
    consumer: ConsumerProgressView


_UNKNOWN_READINESS = AuthorityReadinessView(
    ready=False, state="unknown", reason="authority evaluator unavailable"
)


def project_authority(
    *,
    build_sha: str,
    ownership_enabled: bool,
    ownership: OwnershipSnapshot | None,
    token_mint: dict[str, object] | None,
    health: IngestionHealthState | None,
    health_stale: bool,
    authority: LossDetectionResult | None,
) -> MarketAuthorityDiagnostics:
    """Build the diagnostics response from already-fetched read-only signals (no I/O)."""
    return MarketAuthorityDiagnostics(
        build_sha=build_sha,
        ownership_enabled=ownership_enabled,
        ownership=_ownership_view(ownership),
        token_mint=_token_mint_view(token_mint),
        ingestion_health=_health_view(health, health_stale),
        authority=_authority_view(authority),
        consumer=_consumer_view(authority),
    )


def _ownership_view(snapshot: OwnershipSnapshot | None) -> OwnershipView:
    if snapshot is None:
        return OwnershipView(
            has_owner=False, owner_role=None, instance_id=None, fencing_generation=None
        )
    return OwnershipView(
        has_owner=snapshot.has_owner,
        owner_role=snapshot.owner_role,
        instance_id=snapshot.instance_id,
        fencing_generation=snapshot.fencing_generation,
    )


def _token_mint_view(record: dict[str, object] | None) -> TokenMintView:
    if not record:
        return TokenMintView(
            recorded=False,
            last_mint_at_ms=None,
            owner_role=None,
            instance_id=None,
            fencing_generation=None,
        )
    return TokenMintView(
        recorded=True,
        last_mint_at_ms=_as_int(record.get("last_mint_at_ms")),
        owner_role=_as_str(record.get("owner_role")),
        instance_id=_as_str(record.get("instance_id")),
        fencing_generation=_as_int(record.get("fencing_generation")),
    )


def _health_view(health: IngestionHealthState | None, stale: bool) -> IngestionHealthView:
    if health is None:
        return IngestionHealthView(
            present=False,
            stale=True,
            producer_id=None,
            producer_epoch=None,
            ingestion=None,
            transport=None,
            last_published_sequence=None,
            terminal_publication_break=None,
            publication_outcome_uncertain=None,
            updated_at=None,
        )
    return IngestionHealthView(
        present=True,
        stale=stale,
        producer_id=health.producer_id,
        producer_epoch=health.producer_epoch,
        ingestion=health.ingestion.value,
        transport=health.transport.value,
        last_published_sequence=health.last_published_sequence,
        terminal_publication_break=health.terminal_publication_break,
        publication_outcome_uncertain=health.publication_outcome_uncertain,
        updated_at=health.updated_at,
    )


def _authority_view(result: LossDetectionResult | None) -> AuthorityReadinessView:
    if result is None:
        return _UNKNOWN_READINESS
    return AuthorityReadinessView(
        ready=result.ready_for_authority, state=result.state.value, reason=result.reason
    )


def _consumer_view(result: LossDetectionResult | None) -> ConsumerProgressView:
    if result is None:
        return ConsumerProgressView(
            last_applied_epoch=None, last_applied_sequence=None, stream_length=None, pending=None
        )
    return ConsumerProgressView(
        last_applied_epoch=result.producer_epoch if result.producer_epoch else None,
        last_applied_sequence=result.consumer_last_applied_sequence,
        stream_length=result.stream_length,
        pending=result.pending,
    )


def _as_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float | str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _as_str(value: object) -> str | None:
    return str(value) if value is not None else None
