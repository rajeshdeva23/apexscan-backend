r"""Governed offline generator for the NSE sector membership dataset (SECTOR-2).

Build-time maintenance tool — NOT imported at runtime and does no network I/O. It
reproduces the F&O universe via the repo's own ``derive_equity_fno_universe`` and joins
it to NSE-published classification files that the operator has downloaded to ``WORK``.

Download the source files first (documented in COVERAGE.md), then run::

    PYTHONPATH=backend python -m app.market_intelligence.sector.reference_data.generate \
        <WORK_DIR> <OUT_JSON>

Primary classification = NSE Total Market 'Industry' (fallback NIFTY 500). Secondary =
NSE sectoral/thematic indices. Broad-market = NIFTY 50/500/Total Market. Scoped to the
F&O universe; re-run to regenerate on any universe/classification change (no code edit).
"""

from __future__ import annotations

import csv
import json
import re
import statistics
import sys
from io import StringIO
from pathlib import Path
from typing import Any

from app.adapters.dhan.normalizer import (
    derive_equity_fno_universe,
    normalize_instrument_master,
)

RETRIEVED = "2026-09-02"
VERSION = "2026.09.02"
ARCHIVE = "https://nsearchives.nseindia.com/content/indices"
DHAN_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

_BROAD = "broad_market_index"
_SECTOR = "sector_index"
_THEME = "thematic_index"

# NSE index constituent files present among F&O members -> (id, display name, kind).
INDEX_FILES: dict[str, tuple[str, str, str]] = {
    "ind_nifty50list.csv": ("NIFTY_50", "NIFTY 50", _BROAD),
    "ind_nifty500list.csv": ("NIFTY_500", "NIFTY 500", _BROAD),
    "ind_niftytotalmarket_list.csv": ("NIFTY_TOTAL_MARKET", "NIFTY Total Market", _BROAD),
    "ind_niftybanklist.csv": ("NIFTY_BANK", "NIFTY Bank", _SECTOR),
    "ind_niftyitlist.csv": ("NIFTY_IT", "NIFTY IT", _SECTOR),
    "ind_niftyautolist.csv": ("NIFTY_AUTO", "NIFTY Auto", _SECTOR),
    "ind_niftypharmalist.csv": ("NIFTY_PHARMA", "NIFTY Pharma", _SECTOR),
    "ind_niftymetallist.csv": ("NIFTY_METAL", "NIFTY Metal", _SECTOR),
    "ind_niftyfmcglist.csv": ("NIFTY_FMCG", "NIFTY FMCG", _SECTOR),
    "ind_niftyrealtylist.csv": ("NIFTY_REALTY", "NIFTY Realty", _SECTOR),
    "ind_niftyenergylist.csv": ("NIFTY_ENERGY", "NIFTY Energy", _THEME),
    "ind_niftyfinancelist.csv": ("NIFTY_FIN_SERVICE", "NIFTY Financial Services", _SECTOR),
    "ind_niftymedialist.csv": ("NIFTY_MEDIA", "NIFTY Media", _SECTOR),
    "ind_niftypsubanklist.csv": ("NIFTY_PSU_BANK", "NIFTY PSU Bank", _SECTOR),
    "ind_niftyhealthcarelist.csv": ("NIFTY_HEALTHCARE", "NIFTY Healthcare", _SECTOR),
    "ind_niftyconsumerdurableslist.csv": (
        "NIFTY_CONSUMER_DURABLES",
        "NIFTY Consumer Durables",
        _SECTOR,
    ),
    "ind_niftyoilgaslist.csv": ("NIFTY_OIL_GAS", "NIFTY Oil & Gas", _THEME),
}

Row = dict[str, str]
Json = dict[str, Any]


def _rows(path: Path) -> list[Row]:
    return list(csv.DictReader(StringIO(path.read_text())))


def _sector_id(industry: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", industry.upper()).strip("_")


def _find(work: Path, fname: str) -> Path | None:
    for candidate in (work / fname, work / "idx" / fname):
        if candidate.exists():
            return candidate
    return None


def _primary(work: Path, universe: list[str]) -> tuple[list[Json], dict[str, str], list[str]]:
    src: dict[str, Row] = {}
    for fname in ("ind_niftytotalmarket_list.csv", "ind_nifty500list.csv"):
        for row in _rows(work / fname):
            sym, ind = (row.get("Symbol") or "").strip(), (row.get("Industry") or "").strip()
            if sym and ind:
                src.setdefault(
                    sym,
                    {
                        "industry": ind,
                        "company": (row.get("Company Name") or "").strip(),
                        "isin": (row.get("ISIN Code") or "").strip(),
                        "file": fname,
                    },
                )
    memberships: list[Json] = []
    defs: dict[str, str] = {}
    unmapped: list[str] = []
    for sym in universe:
        hit = src.get(sym)
        if hit is None:
            unmapped.append(sym)
            continue
        sid = _sector_id(hit["industry"])
        defs.setdefault(sid, hit["industry"])
        memberships.append(
            {
                "identity": f"NSE:{sym}",
                "sector_id": sid,
                "primary": True,
                "effective_from": RETRIEVED,
                "source": f"NSE Total Market classification ({hit['file']})",
                "company": hit["company"],
                "isin": hit["isin"],
            }
        )
    return memberships, defs, unmapped


def _secondary(work: Path, fno: set[str]) -> tuple[list[Json], dict[str, Row]]:
    memberships: list[Json] = []
    index_defs: dict[str, Row] = {}
    for fname, (iid, iname, kind) in INDEX_FILES.items():
        path = _find(work, fname)
        if path is None:
            continue
        members = (f"NSE:{(r.get('Symbol') or '').strip()}" for r in _rows(path))
        f_members = sorted(m for m in members if m in fno)
        if not f_members:
            continue
        source = f"NSE index constituents ({fname})"
        index_defs[iid] = {"name": iname, "kind": kind, "source": source}
        for ident in f_members:
            memberships.append(
                {
                    "identity": ident,
                    "sector_id": iid,
                    "primary": False,
                    "effective_from": RETRIEVED,
                    "source": source,
                }
            )
    return memberships, index_defs


def build(work: Path) -> Json:
    """Build the dataset dict from downloaded source files in ``work``."""
    refs = normalize_instrument_master((work / "dhan_scrip.csv").read_text())
    universe = sorted({u.symbol for u in derive_equity_fno_universe(refs).underlyings})
    fno = {f"NSE:{s}" for s in universe}
    primary, sector_defs, unmapped = _primary(work, universe)
    if unmapped:
        raise SystemExit(f"UNMAPPED F&O instruments (fix source, do not guess): {unmapped}")
    secondary, index_defs = _secondary(work, fno)
    notes = (
        f"Primary = NSE Total Market 'Industry' (fallback NIFTY 500) at {ARCHIVE}; "
        f"universe = derive_equity_fno_universe over {DHAN_URL} "
        f"({len(universe)} F&O underlyings); retrieved {RETRIEVED}. "
        "Scoped to F&O; regenerate on universe/classification change."
    )
    return {
        "metadata": {
            "dataset_id": "nse-sector-membership",
            "version": VERSION,
            "effective_from": RETRIEVED,
            "generated_at": RETRIEVED,
            "source_authority": "NSE / NIFTY Indices (published index & classification files)",
            "notes": notes,
        },
        "sector_definitions": [
            {
                "sector_id": sid,
                "name": name,
                "kind": "primary_sector",
                "source": "NSE Total Market classification",
            }
            for sid, name in sorted(sector_defs.items())
        ],
        "index_definitions": [
            {"sector_id": iid, "name": d["name"], "kind": d["kind"], "source": d["source"]}
            for iid, d in sorted(index_defs.items())
        ],
        "memberships": sorted(
            primary + secondary, key=lambda m: (not m["primary"], m["sector_id"], m["identity"])
        ),
    }


def main() -> None:
    """CLI entry point: ``generate <WORK_DIR> <OUT_JSON>`` and print a coverage summary."""
    work, out = Path(sys.argv[1]), Path(sys.argv[2])
    dataset = build(work)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dataset, indent=2) + "\n")
    primary = [m for m in dataset["memberships"] if m["primary"]]
    ids = {m["sector_id"] for m in primary}
    sizes = sorted(len([m for m in primary if m["sector_id"] == sid]) for sid in ids)
    print(
        f"universe={len(primary)} sectors={len(sizes)} smallest={sizes[0]} "
        f"largest={sizes[-1]} median={statistics.median(sizes)} "
        f"index_defs={len(dataset['index_definitions'])} rows={len(dataset['memberships'])}"
    )


if __name__ == "__main__":
    main()
