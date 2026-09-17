"""Pure projection of the market-authority diagnostics (DECOUPLING PHASE H9C-P4, Gate K).

Proves the read-only projector is secret-free and fail-closed: missing/stale evidence never yields
``authority.ready=true`` or a present-and-healthy view, and no token/credential field is ever
produced. No Redis, no I/O — a pure function over already-fetched values.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.market_ingestion.ownership import OwnershipSnapshot
from app.market_ipc.loss_detection import LossDetectionResult, LossDetectionState
from app.market_ipc.state import IngestionHealthState
from app.schemas.market_authority_diagnostics import project_authority
from app.schemas.market_data import ProviderStatus

_NOW = datetime(2026, 9, 17, 10, 15, 30, tzinfo=UTC)


def _snapshot(**overrides: object) -> OwnershipSnapshot:
    base = dict(
        has_owner=True,
        owner_role="ingestion",
        instance_id="inst-abc",
        fencing_generation=7,
        acquire_success_total=0,
        acquire_conflict_total=0,
        renew_success_total=0,
        renew_failure_total=0,
        release_total=0,
        ownership_lost_total=0,
        redis_error_total=0,
    )
    base.update(overrides)
    return OwnershipSnapshot(**base)  # type: ignore[arg-type]


def _health() -> IngestionHealthState:
    return IngestionHealthState(
        producer_id="market-ingestion",
        producer_epoch=3,
        updated_at=_NOW,
        ingestion=ProviderStatus.HEALTHY,
        transport=ProviderStatus.HEALTHY,
        universe_sync=ProviderStatus.UNKNOWN,
        last_published_sequence=42,
        terminal_publication_break=False,
        publication_outcome_uncertain=False,
    )


def _healthy_authority() -> LossDetectionResult:
    return LossDetectionResult(
        state=LossDetectionState.HEALTHY,
        reason="caught up",
        ready_for_authority=True,
        producer_id="market-ingestion",
        producer_epoch=3,
        producer_last_published_sequence=42,
        consumer_last_applied_sequence=42,
        stream_length=42,
        stream_last_generated_id="0-0",
        group_last_delivered_id="0-0",
        pending=0,
    )


def test_all_signals_present_projects_faithfully() -> None:
    result = project_authority(
        build_sha="abc123",
        ownership_enabled=True,
        ownership=_snapshot(),
        token_mint={
            "last_mint_at_ms": 1_700_000_000_000,
            "owner_role": "ingestion",
            "instance_id": "inst-abc",
            "fencing_generation": 7,
        },
        health=_health(),
        health_stale=False,
        authority=_healthy_authority(),
    )
    assert result.build_sha == "abc123"
    assert result.ownership.has_owner and result.ownership.fencing_generation == 7
    assert result.token_mint.recorded and result.token_mint.last_mint_at_ms == 1_700_000_000_000
    assert result.ingestion_health.present and not result.ingestion_health.stale
    assert result.ingestion_health.last_published_sequence == 42
    assert result.authority.ready and result.authority.state == "healthy"
    assert result.consumer.pending == 0


def test_missing_evidence_is_fail_closed() -> None:
    result = project_authority(
        build_sha="abc123",
        ownership_enabled=False,
        ownership=None,
        token_mint=None,
        health=None,
        health_stale=True,
        authority=None,
    )
    assert result.ownership.has_owner is False
    assert result.token_mint.recorded is False
    assert result.ingestion_health.present is False and result.ingestion_health.stale is True
    assert result.authority.ready is False  # missing evaluator → never true
    assert result.authority.state == "unknown"
    assert result.consumer.last_applied_sequence is None


def test_stale_health_is_flagged_not_healthy() -> None:
    result = project_authority(
        build_sha="x",
        ownership_enabled=True,
        ownership=_snapshot(
            has_owner=False, owner_role=None, instance_id=None, fencing_generation=None
        ),
        token_mint=None,
        health=_health(),
        health_stale=True,
        authority=None,
    )
    assert result.ingestion_health.present is True
    assert result.ingestion_health.stale is True  # present but stale — operator can see both


def test_insufficient_authority_is_not_ready() -> None:
    insufficient = LossDetectionResult(
        state=LossDetectionState.INSUFFICIENT_EVIDENCE,
        reason="producer md:health snapshot is missing or stale",
        ready_for_authority=False,
        producer_id="unknown",
        producer_epoch=0,
        producer_last_published_sequence=None,
        consumer_last_applied_sequence=None,
        stream_length=0,
        stream_last_generated_id="0-0",
        group_last_delivered_id="0-0",
        pending=0,
    )
    result = project_authority(
        build_sha="x",
        ownership_enabled=True,
        ownership=None,
        token_mint=None,
        health=None,
        health_stale=True,
        authority=insufficient,
    )
    assert result.authority.ready is False
    assert result.authority.state == "insufficient_evidence"


def test_projection_exposes_no_secret_fields() -> None:
    result = project_authority(
        build_sha="x",
        ownership_enabled=True,
        ownership=_snapshot(),
        token_mint={
            "last_mint_at_ms": 1,
            "owner_role": "backend",
            "instance_id": "i",
            "fencing_generation": 1,
        },
        health=_health(),
        health_stale=False,
        authority=_healthy_authority(),
    )
    dumped = result.model_dump_json().lower()
    # Credential markers must never appear (``token_mint`` is a metadata field name, not a token).
    for secret in ("secret", "totp", "password", "access_token", "client_id", "dhan_pin"):
        assert secret not in dumped
