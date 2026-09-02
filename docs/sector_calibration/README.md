# Sector Calibration — Design (SECTOR-5A)

Design/specification only. This directory freezes **how** ApexScan will calibrate sector
strength, sector confidence, and stock participation from historical Indian F&O data. It
sets **no** production thresholds, weights, scores, or classifications — those are
SECTOR-5D, and only after the SECTOR-5C evidence justifies them.

Subordinate to **ADR-016** (Proposed). No new ADR is required (see METHODOLOGY §"ADR fit").

## Phase decomposition (frozen)

| Phase | Scope | May NOT |
|-------|-------|---------|
| **5A** (this) | methodology, feature/label schemas, data-quality & validation protocol | implement replay; choose thresholds/weights/scores |
| **5B** | historical replay dataset generator (reuses SECTOR-2/3/4 math) | choose thresholds/weights/score; enable live/deploy |
| **5C** | statistical calibration & stability analysis; may *recommend* candidates | silently deploy candidates |
| **5D** | production calibration freeze (epsilon, counts, coverage, SectorScore, confidence, states, stock classes) | proceed without explicit review |

## Documents

- **METHODOLOGY.md** — replay design, no-look-ahead, point-in-time membership, price/timestamp
  semantics, observation grid, primary outcomes, splits/walk-forward, production-math reuse
  rule, and the 20 research questions.
- **FEATURE_SCHEMA.md** — sector / stock / universe feature tables (features known at T).
- **LABEL_SCHEMA.md** — forward labels (returns, direction-aligned, MFE/MAE) measured after T.
- **DATA_QUALITY.md** — typed exclusion reasons, quality assertions, anti-leakage tests,
  bias/risk register.
- **VALIDATION_PROTOCOL.md** — chronological split, walk-forward, day-level dependence,
  effect-size / multiple-testing controls, threshold-robustness, success/failure criteria,
  and the 5B/5C/5D contracts.

## Non-negotiables carried from prior phases

- Everything remains **RAW / UN-CALIBRATED** until SECTOR-5D.
- **No look-ahead**: features at T use only data ≤ T; forward data is labels only.
- **Reuse production math**: replay orchestrates SECTOR-2/3/4; it never re-implements formulas.
- **No index data / no NIFTY** (V1 uses the SECTOR-3 F&O-universe proxy).
- **No ML** in V1; interpretable statistics only.
- **No frontend** — the user owns visual design; a later frontend phase must pause for the user.
- Production (`apexscan-backend:bd0c67f`) and the R4D/B9/B10 track are untouched.
