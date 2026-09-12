"""Unit tests for the Phase-H flag matrix, illegal-combo validation, and mode derivation (H1).

Exercises the ADR-025 rules directly on the pure `app.market_ingestion.mode` module (no Settings,
no env, no I/O): the default shape is LEGACY_ONLY, each future rollout shape derives its mode, and
every frozen illegal combination fails fast.
"""

from __future__ import annotations

import pytest

from app.market_ingestion.mode import (
    MarketPathMode,
    PhaseHConfigError,
    PhaseHFlags,
    derive_market_path_mode,
    validate_phase_h_flags,
)


def _flags(
    *,
    ingestion: bool = False,
    publisher: bool = False,
    consumer: bool = False,
    shadow: bool = False,
    authoritative: bool = False,
    legacy: bool = True,
) -> PhaseHFlags:
    return PhaseHFlags(
        market_ingestion_service_enabled=ingestion,
        ipc_publisher_enabled=publisher,
        ipc_consumer_enabled=consumer,
        ipc_shadow_compare_enabled=shadow,
        ipc_authoritative_enabled=authoritative,
        legacy_market_path_enabled=legacy,
    )


# --------------------------------------------------------------------------- #
# Valid shapes → derived mode
# --------------------------------------------------------------------------- #
def test_default_shape_is_legacy_only() -> None:
    assert derive_market_path_mode(_flags()) is MarketPathMode.LEGACY_ONLY


def test_h3_shape_is_ingestion_shadow_publish() -> None:
    flags = _flags(ingestion=True, publisher=True, legacy=True)
    assert derive_market_path_mode(flags) is MarketPathMode.INGESTION_SHADOW_PUBLISH


def test_h4_shape_is_shadow_consume_compare() -> None:
    flags = _flags(ingestion=True, publisher=True, consumer=True, shadow=True, legacy=True)
    assert derive_market_path_mode(flags) is MarketPathMode.SHADOW_CONSUME_COMPARE


def test_h9_shape_is_ipc_authoritative_backend() -> None:
    flags = _flags(ingestion=True, publisher=True, consumer=True, authoritative=True, legacy=False)
    assert derive_market_path_mode(flags) is MarketPathMode.IPC_AUTHORITATIVE_BACKEND


def test_validate_accepts_valid_shapes() -> None:
    for flags in (
        _flags(),
        _flags(ingestion=True, publisher=True),
        _flags(ingestion=True, publisher=True, consumer=True, shadow=True),
        _flags(ingestion=True, publisher=True, consumer=True, authoritative=True, legacy=False),
    ):
        validate_phase_h_flags(flags)  # must not raise


# --------------------------------------------------------------------------- #
# Illegal combinations → fail fast (ADR-025)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("flags", "needle"),
    [
        (_flags(consumer=False, authoritative=True, legacy=False), "IPC_CONSUMER_ENABLED"),
        (_flags(consumer=True, authoritative=True, legacy=True), "mutually exclusive"),
        (_flags(consumer=False, shadow=True), "IPC_CONSUMER_ENABLED"),
        (_flags(ingestion=False, publisher=True), "MARKET_INGESTION_SERVICE_ENABLED"),
        (_flags(legacy=False, authoritative=False), "no market authority"),
        (_flags(consumer=True, shadow=False, authoritative=False), "poisoning dedup"),
    ],
)
def test_illegal_combinations_are_rejected(flags: PhaseHFlags, needle: str) -> None:
    with pytest.raises(PhaseHConfigError) as excinfo:
        validate_phase_h_flags(flags)
    assert needle in str(excinfo.value)


def test_derive_also_validates() -> None:
    with pytest.raises(PhaseHConfigError):
        derive_market_path_mode(_flags(legacy=False, authoritative=False))
