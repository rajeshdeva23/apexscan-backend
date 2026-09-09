"""Unit tests for the dynamic F&O universe (DECOUPLING PHASE E).

Covers snapshot determinism/immutability/content-hash, deterministic diff, resolver behaviour
(missing/conflicting provider mapping, unclassified sector, unresolved surfacing), ingestion and
backend views + their agreement, diagnostics, and the weekend-aware next-trading-date helper.
File-store persistence/restart/concurrency is in the integration suite (tmp_path).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from app.market_engine.session import TradingCalendar
from app.market_universe import (
    CANDIDATE_VERSION,
    EmptyUniverseError,
    InMemoryProviderMappingSource,
    PromotionValidationError,
    ProviderMapping,
    SnapshotState,
    SourceProvenance,
    UniverseMetrics,
    UniverseResolver,
    UnresolvedReason,
    build_universe_diagnostics,
    diff_snapshots,
    next_trading_date,
    to_backend_universe,
    to_subscription_universe,
    validate_promotable,
)
from app.schemas.market_data import Instrument

_TD = date(2026, 9, 10)
_EFFECTIVE_AT = datetime(2026, 9, 10, 3, 45, tzinfo=UTC)


def _inst(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _mapping(security_id: str, segment: str = "NSE_FNO") -> ProviderMapping:
    return ProviderMapping(provider_security_id=security_id, exchange_segment=segment)


def _provenance(fno_version: str = "2026.09.10") -> SourceProvenance:
    return SourceProvenance(
        fno_source="nse-fno-membership",
        fno_version=fno_version,
        fno_effective_date=_TD,
        instrument_master_source="dhan-scrip-master",
        instrument_master_version="2026-09-10",
        sector_dataset_id="nse-sectors",
        sector_version="2026.09.02",
    )


class _Sector:
    """Test sector authority: identity -> sector id (or absent = unclassified)."""

    def __init__(self, memberships: dict[str, str]) -> None:
        self._memberships = memberships

    def resolve_primary(self, identity: str, on: date | None = None) -> str | None:
        return self._memberships.get(identity)


def _resolver(
    mappings: dict[Instrument, ProviderMapping], sectors: dict[str, str]
) -> UniverseResolver:
    return UniverseResolver(
        mapping_source=InMemoryProviderMappingSource(mappings),
        sector_authority=_Sector(sectors),
    )


def _resolve(resolver: UniverseResolver, membership: list[Instrument], **overrides: object):
    return resolver.resolve(
        membership,
        trading_date=overrides.get("trading_date", _TD),  # type: ignore[arg-type]
        effective_at=_EFFECTIVE_AT,
        provenance=overrides.get("provenance", _provenance()),  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# determinism, immutability, content hash
# --------------------------------------------------------------------------- #
def test_snapshot_is_deterministic_regardless_of_input_order() -> None:
    mappings = {_inst("A"): _mapping("1"), _inst("B"): _mapping("2"), _inst("C"): _mapping("3")}
    sectors = {"NSE:A": "IT", "NSE:B": "BANK", "NSE:C": "IT"}
    resolver = _resolver(mappings, sectors)
    first = _resolve(resolver, [_inst("A"), _inst("B"), _inst("C")])
    second = _resolve(resolver, [_inst("C"), _inst("A"), _inst("B")])
    assert first.candidate.content_sha256 == second.candidate.content_sha256
    assert first.candidate.identities == ("NSE:A", "NSE:B", "NSE:C")


def test_snapshot_is_immutable() -> None:
    resolver = _resolver({_inst("A"): _mapping("1")}, {"NSE:A": "IT"})
    snapshot = _resolve(resolver, [_inst("A")]).candidate
    with pytest.raises(ValidationError):
        snapshot.universe_version = 5  # type: ignore[misc]


def test_membership_mapping_sector_changes_change_the_hash() -> None:
    base = _resolve(_resolver({_inst("A"): _mapping("1")}, {"NSE:A": "IT"}), [_inst("A")])
    membership_change = _resolve(
        _resolver({_inst("A"): _mapping("1"), _inst("B"): _mapping("2")}, {"NSE:A": "IT"}),
        [_inst("A"), _inst("B")],
    )
    mapping_change = _resolve(
        _resolver({_inst("A"): _mapping("999")}, {"NSE:A": "IT"}), [_inst("A")]
    )
    sector_change = _resolve(
        _resolver({_inst("A"): _mapping("1")}, {"NSE:A": "BANK"}), [_inst("A")]
    )
    shas = {
        base.candidate.content_sha256,
        membership_change.candidate.content_sha256,
        mapping_change.candidate.content_sha256,
        sector_change.candidate.content_sha256,
    }
    assert len(shas) == 4  # every logical change yields a distinct fingerprint


def test_same_logical_universe_same_hash() -> None:
    a = _resolve(_resolver({_inst("A"): _mapping("1")}, {"NSE:A": "IT"}), [_inst("A")])
    b = _resolve(_resolver({_inst("A"): _mapping("1")}, {"NSE:A": "IT"}), [_inst("A")])
    assert a.candidate.content_sha256 == b.candidate.content_sha256


# --------------------------------------------------------------------------- #
# resolver: mapping + sector classification
# --------------------------------------------------------------------------- #
def test_missing_provider_mapping_is_surfaced_not_dropped() -> None:
    resolver = _resolver({_inst("A"): _mapping("1")}, {"NSE:A": "IT"})
    result = _resolve(resolver, [_inst("A"), _inst("B")])  # B has no mapping
    assert result.candidate.identities == ("NSE:A",)
    assert [u.identity for u in result.unresolved] == ["NSE:B"]
    assert result.unresolved[0].reason is UnresolvedReason.MISSING_PROVIDER_MAPPING
    assert not result.is_promotable


def test_conflicting_provider_mapping_is_surfaced() -> None:
    resolver = _resolver(
        {_inst("A"): _mapping("dup"), _inst("B"): _mapping("dup")}, {"NSE:A": "IT", "NSE:B": "IT"}
    )
    result = _resolve(resolver, [_inst("A"), _inst("B")])
    assert result.candidate.instruments == ()  # both excluded (ambiguous security id)
    assert {u.reason for u in result.unresolved} == {UnresolvedReason.CONFLICTING_PROVIDER_MAPPING}
    assert {u.identity for u in result.unresolved} == {"NSE:A", "NSE:B"}


def test_unclassified_sector_is_recorded_not_guessed() -> None:
    resolver = _resolver({_inst("A"): _mapping("1")}, {})  # no sector mapping
    result = _resolve(resolver, [_inst("A")])
    assert result.candidate.instruments[0].sector is None
    assert result.unclassified_sector == ("NSE:A",)


# --------------------------------------------------------------------------- #
# diff (addition / removal / mapping / sector / security-id / rename)
# --------------------------------------------------------------------------- #
def _snapshot(mappings, sectors, membership):
    return _resolve(_resolver(mappings, sectors), membership).candidate


def test_diff_added_removed_unchanged() -> None:
    old = _snapshot(
        {_inst("A"): _mapping("1"), _inst("B"): _mapping("2"), _inst("C"): _mapping("3")},
        {"NSE:A": "IT", "NSE:B": "IT", "NSE:C": "IT"},
        [_inst("A"), _inst("B"), _inst("C")],
    )
    new = _snapshot(
        {_inst("B"): _mapping("2"), _inst("C"): _mapping("3"), _inst("D"): _mapping("4")},
        {"NSE:B": "IT", "NSE:C": "IT", "NSE:D": "IT"},
        [_inst("B"), _inst("C"), _inst("D")],
    )
    diff = diff_snapshots(old, new)
    assert diff.added == ("NSE:D",)
    assert diff.removed == ("NSE:A",)
    assert diff.unchanged == ("NSE:B", "NSE:C")


def test_diff_mapping_changed_is_security_id_change() -> None:
    old = _snapshot({_inst("A"): _mapping("1")}, {"NSE:A": "IT"}, [_inst("A")])
    new = _snapshot({_inst("A"): _mapping("2")}, {"NSE:A": "IT"}, [_inst("A")])
    diff = diff_snapshots(old, new)
    assert diff.mapping_changed == ("NSE:A",)
    assert diff.unchanged == ()


def test_diff_sector_changed() -> None:
    old = _snapshot({_inst("A"): _mapping("1")}, {"NSE:A": "IT"}, [_inst("A")])
    new = _snapshot({_inst("A"): _mapping("1")}, {"NSE:A": "BANK"}, [_inst("A")])
    diff = diff_snapshots(old, new)
    assert diff.sector_changed == ("NSE:A",)


def test_symbol_rename_surfaces_as_add_remove() -> None:
    old = _snapshot({_inst("OLDNAME"): _mapping("1")}, {"NSE:OLDNAME": "IT"}, [_inst("OLDNAME")])
    new = _snapshot({_inst("NEWNAME"): _mapping("1")}, {"NSE:NEWNAME": "IT"}, [_inst("NEWNAME")])
    diff = diff_snapshots(old, new)
    assert diff.added == ("NSE:NEWNAME",) and diff.removed == ("NSE:OLDNAME",)


# --------------------------------------------------------------------------- #
# promotion validation (fail closed)
# --------------------------------------------------------------------------- #
def test_validate_promotable_rejects_unresolved() -> None:
    resolver = _resolver({_inst("A"): _mapping("1")}, {"NSE:A": "IT"})
    result = _resolve(resolver, [_inst("A"), _inst("B")])  # B unresolved
    with pytest.raises(PromotionValidationError):
        validate_promotable(result)


def test_validate_promotable_rejects_empty_universe() -> None:
    resolver = _resolver({}, {})
    result = _resolve(resolver, [])
    with pytest.raises(EmptyUniverseError):
        validate_promotable(result)


def test_candidate_carries_sentinel_version() -> None:
    resolver = _resolver({_inst("A"): _mapping("1")}, {"NSE:A": "IT"})
    candidate = _resolve(resolver, [_inst("A")]).candidate
    assert candidate.state is SnapshotState.CANDIDATE
    assert candidate.universe_version == CANDIDATE_VERSION


# --------------------------------------------------------------------------- #
# views + agreement
# --------------------------------------------------------------------------- #
def test_ingestion_and_backend_views_agree() -> None:
    resolver = _resolver(
        {_inst("A"): _mapping("10"), _inst("B"): _mapping("20")}, {"NSE:A": "IT", "NSE:B": "BANK"}
    )
    snapshot = _resolve(resolver, [_inst("A"), _inst("B")]).candidate.promoted_as(11, _EFFECTIVE_AT)
    subscription = to_subscription_universe(snapshot)
    backend = to_backend_universe(snapshot)
    assert subscription.universe_version == backend.universe_version == 11
    assert tuple(e.identity for e in subscription.entries) == backend.identities
    assert subscription.entries[0].provider_security_id == "10"
    assert backend.sector_membership == {"NSE:A": "IT", "NSE:B": "BANK"}


# --------------------------------------------------------------------------- #
# serialization round-trip + no secrets
# --------------------------------------------------------------------------- #
def test_snapshot_serialization_round_trip() -> None:
    from app.market_universe import UniverseSnapshot

    snapshot = _resolve(
        _resolver({_inst("A"): _mapping("1")}, {"NSE:A": "IT"}), [_inst("A")]
    ).candidate
    restored = UniverseSnapshot.model_validate_json(snapshot.model_dump_json())
    assert restored == snapshot


def test_no_secret_fields_in_serialized_snapshot() -> None:
    snapshot = _resolve(
        _resolver({_inst("A"): _mapping("1")}, {"NSE:A": "IT"}), [_inst("A")]
    ).candidate
    dumped = snapshot.model_dump_json().lower()
    for secret in ("token", "totp", "password", "authorization", "pin", "secret"):
        assert secret not in dumped


# --------------------------------------------------------------------------- #
# next trading date (weekend/holiday aware)
# --------------------------------------------------------------------------- #
def test_next_trading_date_skips_weekend() -> None:
    calendar = TradingCalendar(holidays=[])
    friday = date(2026, 9, 11)  # Friday
    assert next_trading_date(calendar, friday) == date(2026, 9, 14)  # Monday, not Saturday


def test_next_trading_date_skips_holiday() -> None:
    calendar = TradingCalendar(holidays=[date(2026, 9, 11)])
    thursday = date(2026, 9, 10)
    assert next_trading_date(calendar, thursday) == date(2026, 9, 14)  # Fri holiday + weekend


# --------------------------------------------------------------------------- #
# diagnostics
# --------------------------------------------------------------------------- #
def test_diagnostics_report_bounded_counts() -> None:
    resolver = _resolver({_inst("A"): _mapping("1")}, {})  # A unclassified
    result = _resolve(resolver, [_inst("A"), _inst("B")])  # B unresolved
    metrics = UniverseMetrics()
    metrics.record_attempt(_EFFECTIVE_AT)
    metrics.record_success(_EFFECTIVE_AT)
    diagnostics = build_universe_diagnostics(metrics=metrics, result=result, active=None)
    assert diagnostics.instrument_count == 1
    assert diagnostics.unresolved_mapping_count == 1
    assert diagnostics.unclassified_sector_count == 1
    assert diagnostics.current_active_version is None
    assert diagnostics.resolution_successes == 1
