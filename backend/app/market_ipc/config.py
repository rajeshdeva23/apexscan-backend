"""Bounded IPC transport configuration (PHASE A).

A standalone, validated config model. It is deliberately NOT a Settings/env source and is
NOT read by application composition, so merging Phase A cannot activate any transport:
``enabled`` defaults to ``False`` and nothing constructs a live stream from it yet.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MarketIpcConfig(BaseModel):
    """Frozen, bounded configuration for the future Redis Streams IPC transport."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    enabled: bool = False
    stream_name: str = Field(default="md:events", min_length=1, max_length=128)
    consumer_group: str = Field(default="backend", min_length=1, max_length=128)
    consumer_name: str = Field(default="backend-0", min_length=1, max_length=128)
    reference_key_prefix: str = Field(default="md:reference", min_length=1, max_length=128)
    health_key: str = Field(default="md:health", min_length=1, max_length=128)
    maxlen: int = Field(default=100_000, ge=1_000, le=10_000_000)
    read_count: int = Field(default=100, ge=1, le=10_000)
    block_ms: int = Field(default=5_000, ge=0, le=60_000)
    claim_idle_ms: int = Field(default=30_000, ge=1_000, le=600_000)
    dedup_max_entries: int = Field(default=100_000, ge=1_000, le=10_000_000)
    max_payload_bytes: int = Field(default=65_536, ge=256, le=262_144)
    reference_ttl_seconds: int = Field(default=604_800, ge=3_600, le=2_592_000)  # 7d; 1h..30d

    @field_validator(
        "stream_name",
        "consumer_group",
        "consumer_name",
        "reference_key_prefix",
        "health_key",
    )
    @classmethod
    def _no_whitespace(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError("Redis key/group/consumer names must not contain whitespace")
        return value
