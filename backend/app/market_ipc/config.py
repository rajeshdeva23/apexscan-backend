"""Bounded IPC transport configuration (PHASE A).

A standalone, validated config model. It is deliberately NOT a Settings/env source and is
NOT read by application composition, so merging Phase A cannot activate any transport:
``enabled`` defaults to ``False`` and nothing constructs a live stream from it yet.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MarketIpcConfig(BaseModel):
    """Frozen, bounded configuration for the future Redis Streams IPC transport."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    enabled: bool = False
    stream_name: str = Field(default="md:events", min_length=1, max_length=128)
    consumer_group: str = Field(default="backend", min_length=1, max_length=128)
    consumer_name: str = Field(default="backend-0", min_length=1, max_length=128)
    reference_key_prefix: str = Field(default="md:reference", min_length=1, max_length=128)
    health_key: str = Field(default="md:health", min_length=1, max_length=128)
    # md:health conveyance (H9B). ``health_ttl_seconds`` bounds how long an unrefreshed snapshot
    # survives in Redis; ``health_stale_seconds`` is the reader's fail-closed freshness deadline —
    # a snapshot older than this yields no producer evidence (B11 fails closed). Keep the TTL >=
    # the staleness deadline so a still-fresh snapshot is never evicted before it is considered
    # stale. Off with everything else until composed.
    health_ttl_seconds: int = Field(default=30, ge=1, le=3_600)
    health_stale_seconds: float = Field(default=15.0, gt=0, le=3_600)
    maxlen: int = Field(default=100_000, ge=1_000, le=10_000_000)
    read_count: int = Field(default=100, ge=1, le=10_000)
    block_ms: int = Field(default=5_000, ge=0, le=60_000)
    claim_idle_ms: int = Field(default=30_000, ge=1_000, le=600_000)
    dedup_max_entries: int = Field(default=100_000, ge=1_000, le=10_000_000)
    # C1 durable consumer idempotency (off with everything else; never activates IPC).
    dedup_key_prefix: str = Field(default="md:dedup", min_length=1, max_length=128)
    dedup_ttl_seconds: int = Field(default=86_400, ge=3_600, le=2_592_000)  # 1d; 1h..30d
    # B4 retention invariant (ADR-028): the stream is trimmed by AGE to this horizon, so an event
    # cannot be redelivered/reclaimed once older; the dedup key must outlive that horizon (+ margin)
    # or an expired dedup identity could let a still-redeliverable event re-apply. TIME-based, not a
    # MAXLEN count (which is event-rate-dependent and cannot bound a horizon in time).
    max_redelivery_horizon_seconds: int = Field(default=43_200, ge=1, le=2_592_000)  # 12h; 1s..30d
    retention_safety_margin_seconds: int = Field(default=3_600, ge=0, le=2_592_000)  # 1h
    max_payload_bytes: int = Field(default=65_536, ge=256, le=262_144)
    reference_ttl_seconds: int = Field(default=604_800, ge=3_600, le=2_592_000)  # 7d; 1h..30d
    # M2 async publication boundary (off by default with everything else; never activates IPC).
    publish_queue_capacity: int = Field(default=10_000, ge=1, le=1_000_000)
    publish_shutdown_drain_timeout_seconds: float = Field(default=5.0, ge=0.0, le=300.0)

    @field_validator(
        "stream_name",
        "consumer_group",
        "consumer_name",
        "reference_key_prefix",
        "health_key",
        "dedup_key_prefix",
    )
    @classmethod
    def _no_whitespace(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError("Redis key/group/consumer names must not contain whitespace")
        return value

    @model_validator(mode="after")
    def _dedup_outlives_redelivery_horizon(self) -> Self:
        """Fail closed at construction unless the B4 retention invariant holds (ADR-028)."""
        validate_retention_invariant(self)
        return self

    @model_validator(mode="after")
    def _health_ttl_outlives_staleness(self) -> Self:
        """Require the md:health TTL to be >= the reader's staleness deadline (H9B)."""
        if self.health_ttl_seconds < self.health_stale_seconds:
            raise ValueError(
                "health_ttl_seconds "
                f"({self.health_ttl_seconds}) must be >= health_stale_seconds "
                f"({self.health_stale_seconds}) so a still-fresh md:health snapshot is not evicted "
                "before the reader would consider it stale."
            )
        return self


def validate_retention_invariant(config: MarketIpcConfig) -> None:
    """Raise unless the dedup key is guaranteed to outlive every legitimate redelivery (B4).

    Invariant: ``dedup_ttl_seconds >= max_redelivery_horizon_seconds +
    retention_safety_margin_seconds``. The consumer refuses to apply an event older than the
    horizon, so an event is never redelivered/applied past it; the dedup key must still exist for
    every reclaim *within* the horizon (plus a margin covering producer↔consumer clock skew and
    apply-vs-publish delay), or a within-horizon redelivery would miss the dedup gate and re-apply.

    Enforced both at config construction (the model validator) and at consumer start (so a config
    mutated via ``model_copy`` — which bypasses field/model validation — can still never start the
    IPC consumer/authority path with an unsafe retention window).
    """
    required = config.max_redelivery_horizon_seconds + config.retention_safety_margin_seconds
    if config.dedup_ttl_seconds < required:
        raise ValueError(
            "B4 retention invariant violated: dedup_ttl_seconds "
            f"({config.dedup_ttl_seconds}) must be >= max_redelivery_horizon_seconds "
            f"({config.max_redelivery_horizon_seconds}) + retention_safety_margin_seconds "
            f"({config.retention_safety_margin_seconds}) = {required}. Raise dedup_ttl_seconds "
            "or lower the horizon/margin; a dedup key must outlive every legitimate redelivery."
        )
