# Sector Validation — SECTOR-VALIDATION-1 (shadow, offline)

Read-only validation that the merged **SECTOR-2** (membership), **SECTOR-3** (metrics), and
**SECTOR-4** (stock participation) produce internally consistent, deterministic, explainable
outputs over the real F&O universe. This is **evidence only** — it answers *"are the outputs
correct/consistent?"*, **not** *"do they predict returns?"* (that is SECTOR-5B/5C).

**No** SectorScore, confidence, thresholds, `direction_epsilon` calibration, hysteresis, ML,
trading, strategy filtering, live wiring, or frontend. Subordinate to **ADR-016** (Proposed;
unchanged).

## What was built

- **Whole-universe harness** `app/tools/sector_validation/` (evidence tooling, NOT
  runtime-imported): `evaluate_universe()` orchestrates the merged pure functions
  (`MembershipResolver` → `calculate_universe_proxy` → `calculate_sector_metrics` →
  `rank_sector_constituents`) over a snapshot; it **re-implements no metric** (so validation
  and production cannot diverge). Plus `to_artifact()` (§VALIDATION_ARTIFACT_SCHEMA) and
  `ValidationObservation` input.
- **Tests** `tests/unit/tools/test_sector_validation.py` (9): full-universe membership
  integrity + reconciliation, unmapped fail-closed, coverage, heavyweight, N=1, determinism/
  order-invariance, stale exclusion, duplicate/mixed-date fail-closed, relative-strength.
  Primitive math (median/MAD/IQR/breadth/symmetry/scale/order, ranking/percentile/alignment)
  is **already** covered by `test_sector_metrics.py` (35) and `test_stock_participation.py`
  (30) and is **not duplicated** here.

## Headline results (2026-09-02 baseline dataset)

- Universe **210** mapped across **18** primary sectors; per-sector totals reconcile exactly
  to 210; every identity in exactly one primary sector; 0 unmapped; TEXTILES N=1,
  FINANCIAL_SERVICES N=55.
- Heavyweight-resistant, coverage-correct, stale/missing excluded (never zero-return),
  bull/bear-symmetric, order/scale-invariant, N=1 carries no confidence claim.
- **Performance:** full-universe evaluation (18 sectors + all rankings + proxy over 210) =
  **median 6.6 ms, p95 7.1 ms, max 8.8 ms** (50 local runs) — trivial for a per-minute cadence.

## Documents

- **SECTOR_METRICS_VALIDATION.md** — SECTOR-3 checks + where each is proven.
- **STOCK_PARTICIPATION_VALIDATION.md** — SECTOR-4 checks.
- **LIVE_SHADOW_VALIDATION_PLAN.md** — how to feed real live observations safely (future,
  requires explicit authorization; passive EventBus subscriber, no 2nd feed/auth).
- **VALIDATION_ARTIFACT_SCHEMA.md** — the evidence artifact `to_artifact()` emits.

Verdict: **SECTOR-VALIDATION-1 PASS** — pure logic validated; ready for controlled
live-shadow *implementation review* (not executed here).
