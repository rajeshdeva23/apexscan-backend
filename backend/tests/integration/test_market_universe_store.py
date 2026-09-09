"""Persistence integration for the file-based UniverseSnapshot store (DECOUPLING PHASE E).

Uses a disposable local filesystem (tmp_path) — never a production DB/Redis. Verifies durable
candidate/promoted persistence, monotonic + concurrency-safe version allocation, effective-date
selection (never "latest created"), addition/removal/re-addition across versions, restart
recovery, weekend holding, and fail-closed promotion.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.market_universe import (
    EmptyUniverseError,
    FileUniverseSnapshotStore,
    InMemoryProviderMappingSource,
    PromotionValidationError,
    ProviderMapping,
    SnapshotState,
    SourceProvenance,
    UniverseResolver,
)
from app.schemas.market_data import Instrument

_EFFECTIVE_AT = datetime(2026, 9, 10, 3, 45, tzinfo=UTC)


def _inst(symbol: str) -> Instrument:
    return Instrument(exchange="NSE", symbol=symbol)


def _sectors(*identities: str) -> dict[str, str]:
    return {identity: "IT" for identity in identities}


class _Sector:
    def __init__(self, memberships: dict[str, str]) -> None:
        self._memberships = memberships

    def resolve_primary(self, identity: str, on: date | None = None) -> str | None:
        return self._memberships.get(identity)


def _provenance(trading_date: date, version: str) -> SourceProvenance:
    return SourceProvenance(
        fno_source="nse-fno-membership",
        fno_version=version,
        fno_effective_date=trading_date,
        instrument_master_source="dhan-scrip-master",
        instrument_master_version=version,
        sector_dataset_id="nse-sectors",
        sector_version="2026.09.02",
    )


def _resolve(symbols: list[str], *, trading_date: date, version: str = "v"):
    mappings = {
        _inst(s): ProviderMapping(provider_security_id=s, exchange_segment="NSE_FNO")
        for s in symbols
    }
    sectors = _sectors(*(f"NSE:{s}" for s in symbols))
    resolver = UniverseResolver(
        mapping_source=InMemoryProviderMappingSource(mappings), sector_authority=_Sector(sectors)
    )
    return resolver.resolve(
        [_inst(s) for s in symbols],
        trading_date=trading_date,
        effective_at=_EFFECTIVE_AT,
        provenance=_provenance(trading_date, version),
    )


def _store(tmp_path: Path) -> FileUniverseSnapshotStore:
    return FileUniverseSnapshotStore(root=tmp_path / "universe")


# --------------------------------------------------------------------------- #
# candidate / promotion / version
# --------------------------------------------------------------------------- #
def test_candidate_persist_and_promote(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = _resolve(["A", "B"], trading_date=date(2026, 9, 10))
    store.save_candidate(result.candidate)
    assert store.get_candidate(result.candidate.content_sha256) == result.candidate
    assert store.active_for(date(2026, 9, 10)) is None  # candidate is not active

    promoted = store.promote(result, effective_at=_EFFECTIVE_AT)
    assert promoted.state is SnapshotState.PROMOTED
    assert promoted.universe_version == 1
    assert store.active_for(date(2026, 9, 10)).universe_version == 1


def test_version_is_monotonic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    v1 = store.promote(_resolve(["A"], trading_date=date(2026, 9, 10)), effective_at=_EFFECTIVE_AT)
    v2 = store.promote(
        _resolve(["A", "B"], trading_date=date(2026, 9, 11)), effective_at=_EFFECTIVE_AT
    )
    assert (v1.universe_version, v2.universe_version) == (1, 2)
    assert store.list_versions() == (1, 2)


def test_identical_content_repromotion_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.promote(
        _resolve(["A"], trading_date=date(2026, 9, 10)), effective_at=_EFFECTIVE_AT
    )
    again = store.promote(
        _resolve(["A"], trading_date=date(2026, 9, 10)), effective_at=_EFFECTIVE_AT
    )
    assert first.universe_version == again.universe_version == 1  # no new version for same content
    assert store.list_versions() == (1,)


def test_concurrent_promotion_allocates_distinct_versions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # distinct content per task (distinct trading dates) -> distinct monotonic versions, no dupes
    results = [_resolve(["A"], trading_date=date(2026, 9, d)) for d in range(1, 21)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        promoted = list(pool.map(lambda r: store.promote(r, effective_at=_EFFECTIVE_AT), results))
    versions = sorted(p.universe_version for p in promoted)
    assert versions == list(range(1, 21))  # 20 distinct versions, none reused


# --------------------------------------------------------------------------- #
# effective-date selection
# --------------------------------------------------------------------------- #
def test_active_for_selects_by_effective_date_not_creation_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # promote the Sep-10-effective snapshot FIRST, then the Sep-9-effective one
    store.promote(_resolve(["A", "B"], trading_date=date(2026, 9, 10)), effective_at=_EFFECTIVE_AT)
    store.promote(_resolve(["A"], trading_date=date(2026, 9, 9)), effective_at=_EFFECTIVE_AT)
    assert store.active_for(date(2026, 9, 9)).trading_date == date(2026, 9, 9)
    assert store.active_for(date(2026, 9, 10)).trading_date == date(2026, 9, 10)


def test_active_holds_across_weekend(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.promote(
        _resolve(["A"], trading_date=date(2026, 9, 11)), effective_at=_EFFECTIVE_AT
    )  # Fri
    store.promote(
        _resolve(["A", "B"], trading_date=date(2026, 9, 14)), effective_at=_EFFECTIVE_AT
    )  # Mon
    assert store.active_for(date(2026, 9, 12)).trading_date == date(
        2026, 9, 11
    )  # Sat -> Fri snapshot
    assert store.active_for(date(2026, 9, 14)).trading_date == date(2026, 9, 14)  # Mon


def test_active_for_ignores_future_effective_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.promote(_resolve(["A"], trading_date=date(2026, 9, 15)), effective_at=_EFFECTIVE_AT)
    assert store.active_for(date(2026, 9, 14)) is None  # future-effective snapshot is not active


# --------------------------------------------------------------------------- #
# addition / removal / re-addition + restart recovery
# --------------------------------------------------------------------------- #
def test_addition_removal_readdition_across_versions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.promote(_resolve(["ABC"], trading_date=date(2026, 9, 1)), effective_at=_EFFECTIVE_AT)
    store.promote(
        _resolve(["XYZ"], trading_date=date(2026, 9, 2)), effective_at=_EFFECTIVE_AT
    )  # ABC removed
    store.promote(
        _resolve(["ABC", "XYZ"], trading_date=date(2026, 9, 30)), effective_at=_EFFECTIVE_AT
    )  # re-added
    assert "NSE:ABC" not in store.active_for(date(2026, 9, 2)).identities
    assert "NSE:ABC" in store.active_for(date(2026, 9, 30)).identities
    assert store.get_by_version(1).identities == ("NSE:ABC",)  # history retained


def test_restart_recovers_versions_and_active(tmp_path: Path) -> None:
    root = tmp_path / "universe"
    first = FileUniverseSnapshotStore(root=root)
    first.promote(_resolve(["A"], trading_date=date(2026, 9, 9)), effective_at=_EFFECTIVE_AT)
    first.promote(_resolve(["A", "B"], trading_date=date(2026, 9, 10)), effective_at=_EFFECTIVE_AT)

    # simulate a process restart: brand-new store over the same directory
    restarted = FileUniverseSnapshotStore(root=root)
    assert restarted.list_versions() == (1, 2)  # versions did not reset
    assert restarted.active_for(date(2026, 9, 10)).universe_version == 2
    # a new promotion continues monotonically, never reusing a version
    third = restarted.promote(
        _resolve(["A", "B", "C"], trading_date=date(2026, 9, 11)), effective_at=_EFFECTIVE_AT
    )
    assert third.universe_version == 3


# --------------------------------------------------------------------------- #
# fail-closed promotion + active preserved on failure
# --------------------------------------------------------------------------- #
def test_promote_unresolved_fails_and_keeps_active(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.promote(_resolve(["A"], trading_date=date(2026, 9, 9)), effective_at=_EFFECTIVE_AT)
    # a candidate with a missing provider mapping (B has none)
    mappings = {_inst("A"): ProviderMapping(provider_security_id="A", exchange_segment="NSE_FNO")}
    resolver = UniverseResolver(
        mapping_source=InMemoryProviderMappingSource(mappings), sector_authority=_Sector({})
    )
    bad = resolver.resolve(
        [_inst("A"), _inst("B")],
        trading_date=date(2026, 9, 10),
        effective_at=_EFFECTIVE_AT,
        provenance=_provenance(date(2026, 9, 10), "v2"),
    )
    with pytest.raises(PromotionValidationError):
        store.promote(bad, effective_at=_EFFECTIVE_AT)
    assert store.active_for(date(2026, 9, 10)).universe_version == 1  # active untouched
    assert store.list_versions() == (1,)


def test_promote_empty_universe_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    empty = _resolve([], trading_date=date(2026, 9, 10))
    with pytest.raises(EmptyUniverseError):
        store.promote(empty, effective_at=_EFFECTIVE_AT)
    assert store.list_versions() == ()
