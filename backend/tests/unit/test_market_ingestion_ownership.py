"""Single-owner lease config validation + import purity (PHASE H9A, ADR-030).

The Redis lease/fencing behaviour is proven over real Redis in the integration suite. This module
covers the pure, deterministic parts: the fail-closed lease-timing invariant and import purity.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.market_ingestion.ownership import OwnershipLeaseConfig


def test_h9a_t16_defaults_are_valid() -> None:
    config = OwnershipLeaseConfig()
    assert 0 < config.renewal_interval_seconds < config.lease_ttl_seconds
    assert config.owner_key == "md:provider:ownership"
    assert config.fence_key == "md:provider:ownership:fence"


def test_h9a_t16_renewal_below_ttl_accepted() -> None:
    config = OwnershipLeaseConfig(lease_ttl_seconds=30, renewal_interval_seconds=10)
    assert config.renewal_interval_seconds == 10


def test_h9a_t17_renewal_equal_ttl_rejected() -> None:
    with pytest.raises(ValidationError, match="ownership lease invariant"):
        OwnershipLeaseConfig(lease_ttl_seconds=10, renewal_interval_seconds=10)


def test_h9a_t17_renewal_above_ttl_rejected() -> None:
    with pytest.raises(ValidationError, match="ownership lease invariant"):
        OwnershipLeaseConfig(lease_ttl_seconds=10, renewal_interval_seconds=11)


def test_h9a_t16_invalid_ttl_rejected() -> None:
    with pytest.raises(ValidationError):
        OwnershipLeaseConfig(lease_ttl_seconds=0, renewal_interval_seconds=1)  # ge=1


def test_h9a_t17_invalid_renewal_rejected() -> None:
    with pytest.raises(ValidationError):
        OwnershipLeaseConfig(lease_ttl_seconds=30, renewal_interval_seconds=0)  # ge=1


def test_h9a_t16_excessive_ttl_rejected() -> None:
    with pytest.raises(ValidationError):
        OwnershipLeaseConfig(lease_ttl_seconds=3_601, renewal_interval_seconds=10)  # le=3600


def test_h9a_error_message_names_both_quantities() -> None:
    with pytest.raises(ValidationError) as excinfo:
        OwnershipLeaseConfig(lease_ttl_seconds=5, renewal_interval_seconds=5)
    message = str(excinfo.value)
    assert "renewal_interval_seconds" in message
    assert "lease_ttl_seconds" in message


def test_h9a_t18_import_is_side_effect_free() -> None:
    # Importing the ownership module must not connect Redis, start a task, or touch the filesystem.
    import importlib

    module = importlib.import_module("app.market_ingestion.ownership")
    assert hasattr(module, "RedisOwnershipCoordinator")
    assert hasattr(module, "OwnershipLeaseConfig")
    # No module-level Redis client / coordinator instance is constructed at import time.
    assert not any(
        type(value).__name__ in {"Redis", "RedisOwnershipCoordinator"}
        for value in vars(module).values()
    )
