"""Bundled, frozen NSE sector membership dataset and its offline loader.

The runtime loads a validated JSON snapshot from disk — it never fetches from NSE.
Regenerate the snapshot with ``generate.py`` (governed maintenance). See COVERAGE.md
for source provenance and the coverage report.
"""

from __future__ import annotations

from pathlib import Path

from app.market_intelligence.sector.membership import SectorMembershipDataset

_DEFAULT_DATASET = Path(__file__).with_name("nse_sector_membership_2026_09_02.json")


def load_sector_membership_dataset(path: Path | None = None) -> SectorMembershipDataset:
    """Load and structurally validate the bundled membership dataset.

    Args:
        path: Optional override for the dataset JSON; defaults to the bundled snapshot.

    Returns:
        The immutable, parsed dataset. Structural (cross-row) invariants are enforced
        later by ``MembershipResolver``; field-level validation happens here and raises
        on malformed input (fail-closed).
    """
    source = path if path is not None else _DEFAULT_DATASET
    return SectorMembershipDataset.model_validate_json(source.read_text())
