# NSE Sector Membership — Provenance & Coverage

Dataset: `nse_sector_membership_2026_09_02.json` (version `2026.09.02`, effective 2026-09-02).
Regenerate with `generate.py` — the runtime never fetches from NSE (ADR-016 D3).

## Sources (authoritative, published)

| Purpose | Source | File | Retrieved |
|---------|--------|------|-----------|
| F&O universe | Dhan detailed instrument master, reduced by the repo's own `derive_equity_fno_universe` | `https://images.dhan.co/api-data/api-scrip-master-detailed.csv` | 2026-09-02 |
| Primary sector (one per stock) | NSE / NIFTY Indices — `Industry` column | `https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv` (fallback `ind_nifty500list.csv`) | 2026-09-02 |
| Secondary / broad-market | NSE sectoral, thematic & broad index constituents | `ind_nifty{bank,it,auto,pharma,metal,fmcg,realty,energy,finance,media,psubank,healthcare,consumerdurables,oilgas}list.csv`, `ind_nifty50list.csv`, `ind_nifty500list.csv` | 2026-09-02 |

Primary classification uses the NSE **Sector** level (the single `Industry` label per
symbol) — populous enough for breadth (median group size 9), unlike Basic Industry.

## Regeneration

```sh
WORK=/tmp/sector-src
UA='Mozilla/5.0'
curl -s -o "$WORK/dhan_scrip.csv" https://images.dhan.co/api-data/api-scrip-master-detailed.csv
base=https://nsearchives.nseindia.com/content/indices
for f in ind_niftytotalmarket_list ind_nifty500list ind_nifty50list ind_niftybanklist \
         ind_niftyitlist ind_niftyautolist ind_niftypharmalist ind_niftymetallist \
         ind_niftyfmcglist ind_niftyrealtylist ind_niftyenergylist ind_niftyfinancelist \
         ind_niftymedialist ind_niftypsubanklist ind_niftyhealthcarelist \
         ind_niftyconsumerdurableslist ind_niftyoilgaslist; do
  curl -s -H "User-Agent: $UA" -o "$WORK/$f.csv" "$base/$f.csv"
done
PYTHONPATH=backend python -m app.market_intelligence.sector.reference_data.generate \
  "$WORK" backend/app/market_intelligence/sector/reference_data/nse_sector_membership_2026_09_02.json
```

The generator **fails closed**: any F&O underlying missing from the NSE classification
aborts generation (`UNMAPPED ...`) rather than shipping a guess.

## Coverage (2026-09-02)

- F&O underlyings: **210** — mapped **210 / 210**, unmapped **0**.
- Primary sectors: **18**; index definitions: **16**; total membership rows: **847**.
- Group sizes: smallest **1**, largest **55**, median **9**.
- Breadth-thin groups (for later calibration, no threshold set here): `TEXTILES` (1),
  `CONSTRUCTION` (3), `TELECOMMUNICATION` (3), `CONSTRUCTION_MATERIALS` (4).
- Largest: `FINANCIAL_SERVICES` (55) — heavyweight-distortion risk to be handled by the
  V1 metrics engine (equal-weight + breadth + dispersion), not here.
