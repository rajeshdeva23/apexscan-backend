# SECTOR-3 Metrics Validation (SECTOR-VALIDATION-1)

All checks use the merged SECTOR-3 engine verbatim (no re-implementation). "Where proven"
cites the test that establishes it. Validation policy used: `direction_epsilon=0.001`,
`freshness_limit=5m` — **VALIDATION VALUES, NOT PRODUCTION CALIBRATION**.

| Check | Result | Where proven |
|-------|--------|--------------|
| Return decomposition (overnight/intraday/total) | exact Decimal | `test_sector_metrics::test_s3_01_02_03` |
| Gap vs intraday separation (4 sign combos) | separated; gap-up-selloff ≠ intraday-bullish | `test_s3_04_05`, participation `test_s4_38/39` |
| Breadth (adv/dec/unchanged, net∈[-1,1]) | correct; equal-weight, no cap weighting | `test_s3_09..13`, `test_s3_56` |
| Median (odd/even/N=1) | matches independent recompute | `test_s3_14_15`, `test_s3_55` |
| MAD (>0, =0, N=1) | correct; 0 for N=1 | `test_s3_16`, `test_s3_18` |
| IQR (Tukey exclusive hinges, N<2→None) | correct; not numpy/pandas defaults | `test_s3_17`, `test_s3_18` |
| Heavyweight distortion | 1×+5% among flat/neg ⇒ not BULLISH | `test_s3_20`, universe `test_sector_validation::test_heavyweight…` |
| Outlier robustness | median unmoved by +8%/+10% | `test_s3_19`, `test_s3_28` |
| Coverage (valid/expected) | missing lowers coverage, never 0-return | `test_sector_validation::test_coverage_partial…` |
| Freshness boundary (`0 ≤ eval−obs ≤ limit`; future excluded) | inclusive at limit; future not fresh | `test_s3_48`, historical `feature_eligible` |
| Missing / stale excluded (not UNCHANGED) | excluded; coverage drops | `test_s3_37..40`, `test_sector_validation::test_stale…` |
| RawSectorDirection (BULLISH/BEARISH/NEUTRAL/MIXED/INSUFFICIENT) | as implemented; no STRONG_* | `test_s3_26..29` |
| Bull/bear symmetry (median/breadth/MAD/IQR/participation) | mirrors | `test_s3_49..52` |
| Scale invariance | 100→101 == 1000→1010 | `test_s3_54` |
| Order invariance | reorder ⇒ identical | `test_s3_53`, universe `test_determinism_and_order_invariance` |
| Universe proxy (equal-weight median intraday, F&O; **not** NIFTY) | correct | `test_s3_30..32`, `test_relative_strength_uses_universe_proxy` |
| Self-inclusion (sector in whole-universe proxy) | current behavior preserved | see observation below |
| Relative strength (sector median − universe proxy) | correct sign | `test_s3_33..35`, universe test |
| N=1 sector (TEXTILES) | median=value, MAD 0, IQR None, no confidence | `test_sector_validation::test_n1_sector…` |
| Whole-universe reconciliation | Σ per-sector expected == 210; every id one primary | `test_membership_integrity_full_universe` |
| Fail-closed (duplicate, mixed date, membership mismatch, invalid price) | raises typed errors | `test_s3_42..47`, universe fail-closed test |

## Validation observation (for SECTOR-5C, no change here)

**Self-inclusion:** the whole-universe proxy includes the sector's own members. For large
sectors (e.g. FINANCIAL_SERVICES, 55/210 ≈ 26% of the universe) this compresses that sector's
relative-strength magnitude, because the sector materially moves its own benchmark. This is
SECTOR-3's deliberate V1 choice; SECTOR-5C should quantify the compression vs an ex-sector
proxy **for analysis only**. No architecture change in this phase.

## Not done (out of scope)

No `direction_epsilon`/coverage/breadth/relative-strength thresholds chosen; no SectorScore;
no confidence; no predictive/forward analysis.
