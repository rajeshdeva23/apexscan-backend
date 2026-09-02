"""SECTOR-2 tests: domain invariants, resolver determinism, effective dating, dataset.

Covers S2-01..S2-35. Synthetic datasets exercise edge cases; the bundled real dataset
is checked for structural validity, F&O intersection, and deterministic output.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.market_intelligence.sector import (
    GroupKind,
    InstrumentSectorProfile,
    MembershipResolver,
    SectorDatasetError,
    SectorDatasetMetadata,
    SectorDefinition,
    SectorMembership,
    SectorMembershipDataset,
    instrument_identity,
    load_sector_membership_dataset,
)
from app.schemas.market_data import Instrument, InstrumentClass, MarketSegment

D0 = date(2026, 9, 2)


def _meta(**kw: object) -> SectorDatasetMetadata:
    base = dict(
        dataset_id="test",
        version="1",
        effective_from=D0,
        generated_at=D0,
        source_authority="test",
    )
    base.update(kw)
    return SectorDatasetMetadata(**base)  # type: ignore[arg-type]


def _sector(sid: str, kind: GroupKind = GroupKind.PRIMARY_SECTOR) -> SectorDefinition:
    return SectorDefinition(sector_id=sid, name=sid.title(), kind=kind, source="t")


def _index(sid: str, kind: GroupKind = GroupKind.SECTOR_INDEX) -> SectorDefinition:
    return SectorDefinition(sector_id=sid, name=sid, kind=kind, source="t")


def _m(identity: str, sid: str, primary: bool, **kw: object) -> SectorMembership:
    return SectorMembership(
        identity=identity, sector_id=sid, primary=primary, effective_from=D0, source="t", **kw
    )


def _dataset(
    memberships: tuple[SectorMembership, ...],
    sectors: tuple[SectorDefinition, ...] = (),
    indexes: tuple[SectorDefinition, ...] = (),
) -> SectorMembershipDataset:
    return SectorMembershipDataset(
        metadata=_meta(),
        sector_definitions=sectors or (_sector("BANK"), _sector("IT")),
        index_definitions=indexes,
        memberships=memberships,
    )


# --- metadata / definitions (S2-01, S2-02, S2-18, S2-19, S2-20) ---


def test_s2_18_19_dataset_version_and_source_required() -> None:
    with pytest.raises(ValidationError):
        _meta(version="")
    with pytest.raises(ValidationError):
        _meta(source_authority="")


def test_s2_02_duplicate_sector_id_rejected() -> None:
    ds = _dataset((_m("NSE:A", "BANK", True),), sectors=(_sector("BANK"), _sector("BANK")))
    with pytest.raises(SectorDatasetError, match="duplicate sector_id"):
        MembershipResolver(ds)


def test_s2_20_empty_sector_id_rejected() -> None:
    with pytest.raises(ValidationError):
        _sector("")
    with pytest.raises(ValidationError):
        _sector("  BANK ")  # untrimmed whitespace


# --- primary uniqueness / kinds (S2-03, S2-08, S2-06, S2-31, S2-32) ---


def test_s2_03_single_primary_per_instrument_ok() -> None:
    resolver = MembershipResolver(_dataset((_m("NSE:A", "BANK", True),)))
    assert resolver.resolve_primary("NSE:A") == "BANK"


def test_s2_08_multiple_active_primary_rejected() -> None:
    ds = _dataset((_m("NSE:A", "BANK", True), _m("NSE:A", "IT", True)))
    with pytest.raises(SectorDatasetError, match="overlapping active primary"):
        MembershipResolver(ds)


def test_s2_06_membership_undefined_sector_rejected() -> None:
    ds = _dataset((_m("NSE:A", "NOPE", True),))
    with pytest.raises(SectorDatasetError, match="undefined sector"):
        MembershipResolver(ds)


def test_s2_primary_flag_on_index_kind_rejected() -> None:
    ds = _dataset(
        (_m("NSE:A", "NIFTY_BANK", True),),
        sectors=(_sector("BANK"),),
        indexes=(_index("NIFTY_BANK"),),
    )
    with pytest.raises(SectorDatasetError, match="non-primary kind"):
        MembershipResolver(ds)


def test_s2_31_32_secondary_and_broad_do_not_change_primary() -> None:
    ds = _dataset(
        (
            _m("NSE:A", "BANK", True),
            _m("NSE:A", "NIFTY_BANK", False),
            _m("NSE:A", "NIFTY_50", False),
        ),
        sectors=(_sector("BANK"),),
        indexes=(_index("NIFTY_BANK"), _index("NIFTY_50", GroupKind.BROAD_MARKET_INDEX)),
    )
    profile = MembershipResolver(ds).resolve_profile("NSE:A")
    assert profile.primary_sector == "BANK"
    assert profile.secondary_memberships == ("NIFTY_BANK",)
    assert profile.broad_market_memberships == ("NIFTY_50",)


def test_s2_04_secondary_overlap_allowed() -> None:
    ds = _dataset(
        (_m("NSE:A", "NIFTY_BANK", False), _m("NSE:A", "NIFTY_FIN", False)),
        sectors=(_sector("BANK"),),
        indexes=(_index("NIFTY_BANK"), _index("NIFTY_FIN")),
    )
    assert MembershipResolver(ds).secondary_memberships("NSE:A") == ("NIFTY_BANK", "NIFTY_FIN")


# --- duplicates / intervals (S2-05, S2-21, S2-22, S2-23) ---


def test_s2_05_duplicate_membership_row_rejected() -> None:
    ds = _dataset((_m("NSE:A", "BANK", True), _m("NSE:A", "BANK", True)))
    with pytest.raises(SectorDatasetError, match="duplicate membership row"):
        MembershipResolver(ds)


def test_s2_21_invalid_date_interval_rejected() -> None:
    with pytest.raises(ValidationError):
        SectorMembership(
            identity="NSE:A",
            sector_id="BANK",
            primary=True,
            effective_from=D0,
            effective_to=D0,
            source="t",
        )


def test_s2_22_overlapping_primary_intervals_rejected() -> None:
    rows = (
        _m("NSE:A", "BANK", True, effective_to=date(2026, 12, 31)),
        SectorMembership(
            identity="NSE:A",
            sector_id="IT",
            primary=True,
            effective_from=date(2026, 6, 1),
            source="t",
        ),
    )
    with pytest.raises(SectorDatasetError, match="overlapping active primary"):
        MembershipResolver(_dataset(rows))


def test_s2_23_non_overlapping_history_accepted() -> None:
    rows = (
        _m("NSE:A", "BANK", True, effective_to=date(2026, 12, 31)),
        SectorMembership(
            identity="NSE:A",
            sector_id="IT",
            primary=True,
            effective_from=date(2026, 12, 31),
            source="t",
        ),
    )
    resolver = MembershipResolver(_dataset(rows))
    assert resolver.resolve_primary("NSE:A", date(2026, 10, 1)) == "BANK"
    assert resolver.resolve_primary("NSE:A", date(2027, 1, 1)) == "IT"


# --- effective dating (S2-09, S2-10, S2-11, S2-12, S2-13) ---


def test_s2_09_10_11_effective_date_boundaries() -> None:
    rows = (
        SectorMembership(
            identity="NSE:A",
            sector_id="BANK",
            primary=True,
            effective_from=date(2026, 9, 2),
            source="t",
        ),
    )
    resolver = MembershipResolver(_dataset(rows))
    assert resolver.resolve_primary("NSE:A", date(2026, 9, 1)) is None  # before
    assert resolver.resolve_primary("NSE:A", date(2026, 9, 2)) == "BANK"  # on
    assert resolver.resolve_primary("NSE:A", date(2026, 9, 3)) == "BANK"  # after


def test_s2_12_13_replacement_activates_without_backward_leak() -> None:
    rows = (
        _m("NSE:A", "BANK", True, effective_to=date(2026, 10, 1)),
        SectorMembership(
            identity="NSE:A",
            sector_id="IT",
            primary=True,
            effective_from=date(2026, 10, 1),
            source="t",
        ),
    )
    resolver = MembershipResolver(_dataset(rows))
    assert resolver.resolve_primary("NSE:A", date(2026, 9, 15)) == "BANK"
    assert resolver.resolve_primary("NSE:A", date(2026, 10, 2)) == "IT"


# --- resolver behaviour (S2-07, S2-14, S2-15, S2-16, S2-17, S2-30) ---


def test_s2_07_15_missing_primary_is_explicit_failclosed() -> None:
    resolver = MembershipResolver(_dataset((_m("NSE:A", "BANK", True),)))
    profile = resolver.resolve_profile("NSE:UNKNOWN")
    assert profile.primary_sector is None
    assert profile.is_mapped is False
    assert resolver.resolve_primary("NSE:UNKNOWN") is None


def test_s2_14_canonical_identity_matches_marketcontext_key() -> None:
    instrument = Instrument(
        exchange="NSE",
        symbol="RELIANCE",
        market_segment=MarketSegment.EQUITY,
        instrument_class=InstrumentClass.CASH,
    )
    assert instrument_identity(instrument) == "NSE:RELIANCE"


def test_s2_16_17_members_and_profile_deterministic() -> None:
    ds = _dataset((_m("NSE:B", "BANK", True), _m("NSE:A", "BANK", True)))
    resolver = MembershipResolver(ds)
    assert resolver.members_of_primary_sector("BANK") == ("NSE:A", "NSE:B")
    assert resolver.all_primary_sectors() == ("BANK", "IT")


def test_s2_30_models_immutable() -> None:
    profile = InstrumentSectorProfile(identity="NSE:A", primary_sector="BANK")
    with pytest.raises(ValidationError):
        profile.identity = "NSE:B"  # type: ignore[misc]


# --- dataset load + real F&O intersection (S2-24, S2-25, S2-26, S2-33, S2-34, S2-35) ---


def test_s2_33_34_bundled_dataset_loads_and_malformed_failclosed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    dataset = load_sector_membership_dataset()
    assert MembershipResolver(dataset).all_primary_sectors()  # validates clean
    bad = tmp_path / "bad.json"
    bad.write_text('{"metadata": {}}')
    with pytest.raises(ValidationError):
        load_sector_membership_dataset(bad)


def test_s2_24_bundled_dataset_full_coverage_no_unmapped_primary() -> None:
    dataset = load_sector_membership_dataset()
    resolver = MembershipResolver(dataset)
    primary_rows = [m for m in dataset.memberships if m.primary]
    assert len(primary_rows) == 210  # F&O universe, 210/210 mapped
    assert all(resolver.resolve_primary(m.identity) is not None for m in primary_rows)


def test_s2_26_group_sizes_deterministic_and_defined() -> None:
    resolver = MembershipResolver(load_sector_membership_dataset())
    sectors = resolver.all_primary_sectors()
    assert len(sectors) == 18
    total = sum(len(resolver.members_of_primary_sector(s)) for s in sectors)
    assert total == 210


def test_s2_35_real_overlap_profile_hdfcbank() -> None:
    resolver = MembershipResolver(load_sector_membership_dataset())
    profile = resolver.resolve_profile("NSE:HDFCBANK")
    assert profile.primary_sector == "FINANCIAL_SERVICES"
    assert "NIFTY_BANK" in profile.secondary_memberships
    assert "NIFTY_50" in profile.broad_market_memberships
