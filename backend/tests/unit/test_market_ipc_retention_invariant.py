"""B4 retention invariant — fail-closed config validation (PHASE H8B, ADR-028).

The durable dedup key must outlive every legitimate redelivery of its event, or an expired dedup
identity lets a still-redeliverable event miss the dedup gate and re-apply. The producer trims the
stream by AGE to ``max_redelivery_horizon_seconds`` (a time bound — a MAXLEN count bound is
event-rate-dependent and cannot bound a horizon in time), so an event stops being redeliverable
once older; the invariant guarantees the dedup key still exists then, plus a margin. An unsafe
configuration must never construct, so it can never start the IPC consumer/authority path.

Invariant: ``dedup_ttl_seconds >= max_redelivery_horizon_seconds +
retention_safety_margin_seconds``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.market_ipc.config import MarketIpcConfig


def _config(**overrides: object) -> MarketIpcConfig:
    return MarketIpcConfig(**overrides)


# T01 — the retention fields exist and the shipped defaults already satisfy the invariant.
def test_h8b_t01_defaults_are_safe_by_construction() -> None:
    config = _config()
    assert config.max_redelivery_horizon_seconds == 43_200
    assert config.retention_safety_margin_seconds == 3_600
    assert config.dedup_ttl_seconds == 86_400
    assert (
        config.dedup_ttl_seconds
        >= config.max_redelivery_horizon_seconds + config.retention_safety_margin_seconds
    )


# T02 — a dedup TTL shorter than horizon+margin is rejected at construction (fail closed).
def test_h8b_t02_ttl_below_horizon_plus_margin_is_rejected() -> None:
    with pytest.raises(ValidationError, match="B4 retention invariant"):
        _config(dedup_ttl_seconds=3_600)  # default horizon 12h + margin 1h => needs 46_800


# T03 — a self-consistent safe configuration is accepted.
def test_h8b_t03_safe_configuration_accepted() -> None:
    config = _config(
        dedup_ttl_seconds=100_000,
        max_redelivery_horizon_seconds=80_000,
        retention_safety_margin_seconds=10_000,
    )
    assert config.dedup_ttl_seconds == 100_000


# T04 — the boundary: exactly equal is safe; one second below is rejected.
def test_h8b_t04_boundary_equal_is_safe_below_is_rejected() -> None:
    ok = _config(
        dedup_ttl_seconds=46_800,
        max_redelivery_horizon_seconds=43_200,
        retention_safety_margin_seconds=3_600,
    )
    assert ok.dedup_ttl_seconds == 46_800
    with pytest.raises(ValidationError, match="B4 retention invariant"):
        _config(
            dedup_ttl_seconds=46_799,
            max_redelivery_horizon_seconds=43_200,
            retention_safety_margin_seconds=3_600,
        )


# T13 — a weekend/holiday-length horizon needs a correspondingly long dedup TTL.
def test_h8b_t13_weekend_horizon_requires_long_dedup_ttl() -> None:
    long_weekend = 4 * 24 * 3_600  # Fri close -> Tue open
    # 1-day TTL cannot cover a 4-day recovery horizon: rejected.
    with pytest.raises(ValidationError, match="B4 retention invariant"):
        _config(dedup_ttl_seconds=86_400, max_redelivery_horizon_seconds=long_weekend)
    # A 5-day TTL covers the 4-day horizon plus the default margin: accepted.
    safe = _config(dedup_ttl_seconds=5 * 24 * 3_600, max_redelivery_horizon_seconds=long_weekend)
    assert safe.max_redelivery_horizon_seconds == long_weekend


# §25 — invalid field values (out of bounds) are rejected regardless of the invariant.
def test_h8b_zero_horizon_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _config(max_redelivery_horizon_seconds=0)  # ge=1


def test_h8b_negative_values_rejected() -> None:
    with pytest.raises(ValidationError):
        _config(max_redelivery_horizon_seconds=-1)
    with pytest.raises(ValidationError):
        _config(retention_safety_margin_seconds=-1)


def test_h8b_excessive_horizon_rejected() -> None:
    with pytest.raises(ValidationError):
        _config(max_redelivery_horizon_seconds=2_592_001)  # le=30d


# The error message names all three quantities so an operator can fix it.
def test_h8b_error_message_is_actionable() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _config(dedup_ttl_seconds=3_600)
    message = str(excinfo.value)
    assert "dedup_ttl_seconds" in message
    assert "max_redelivery_horizon_seconds" in message
    assert "retention_safety_margin_seconds" in message
