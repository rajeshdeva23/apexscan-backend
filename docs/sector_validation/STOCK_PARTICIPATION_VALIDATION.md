# SECTOR-4 Stock-Participation Validation (SECTOR-VALIDATION-1)

Uses the merged SECTOR-4 engine verbatim via the whole-universe harness. No LEADER/
PARTICIPANT/LAGGARD/DIVERGENT production classification and no StockParticipationScore.

| Check | Result | Where proven |
|-------|--------|--------------|
| `stock_vs_sector = intraday − sector_median` | correct; absolute-positive-but-sector-lagging representable | `test_stock_participation::test_s4_01..03`, `test_s4_06_07` |
| `stock_vs_universe = intraday − universe_proxy` | correct; independent of stock_vs_sector | `test_s4_04_05_41` |
| Three dimensions independent (absolute/sector/universe) | preserved | `test_s4_06_07` |
| Alignment (ALIGNED/OPPOSED/NEUTRAL/MIXED_CONTEXT/INSUFFICIENT_DATA) | matches sector raw direction | `test_s4_08..14` |
| Directional strength (+intraday bull / −intraday bear / None otherwise) | correct; bull/bear symmetric | `test_s4_15_16_17` |
| Ranking by directional strength | strongest-in-sector-direction ranks first | `test_s4_18`, `test_s4_37` |
| Competition ranking + ties (10,10,8,7→1,1,3,4) | correct; identity-only display tiebreak | `test_s4_19_20` |
| Percentile `(N−rank)/(N−1)`; ties; N=1→None | correct | `test_s4_21..24`, universe `test_n1_sector…` |
| Robust relative magnitude `(x−median)/MAD`; MAD 0→None; no 0.6745 | correct | `test_s4_25_26_27` |
| Stale stock: eligible=False, reason=STALE, no rank, not UNCHANGED | correct | `test_s4_29`, `test_sector_validation::test_stale…` |
| Missing stock: absent, no rank/alignment, lowers coverage | correct | `test_s4_30`, coverage test |
| Fail-closed (duplicate, wrong sector, wrong date) | typed errors | `test_s4_31..33`, universe fail-closed |
| Bull/bear symmetry, order/scale invariance | mirrors / invariant | `test_s4_34_35_37`, universe determinism |
| Outlier stock ranks top without moving sector median | median unmoved | `test_s4_28`, universe heavyweight |
| Mixed/insufficient sector ⇒ no directional rank | rank/percentile None; metrics still present | `test_s4_13_14` |
| Immutable outputs (no score/confidence/leader fields) | frozen; forbidden fields absent | `test_s4_42_43`, `test_s4_44_49`, universe `test_n1…` artifact scan |

## Whole-universe orchestration result

Over the real 210 universe (`evaluate_universe`): every sector produces a `SectorStockRanking`;
Σ ranked stocks across sectors == 210 (all fresh+valid); directional ranking present only for
BULLISH/BEARISH sectors, absent (metrics-only) for NEUTRAL/MIXED/INSUFFICIENT — as designed.
Determinism + order-invariance confirmed on the full artifact
(`test_determinism_and_order_invariance`).

## Not done

No calibrated stock labels/thresholds; no percentile cutoffs; no forward/predictive analysis
(SECTOR-5C/5D).
