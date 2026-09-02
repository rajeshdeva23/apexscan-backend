# Validation Protocol & Phase Contracts (SECTOR-5A)

## Chronological splitting (no random split)

Market snapshots are serially dependent; random row-level splits leak. Partition **by
trading date** into three contiguous, non-overlapping periods: **development** (threshold
search), **validation** (candidate selection), **untouched out-of-sample test** (final
confirmation, queried once). Exact dates depend on data depth (set in 5B/5C), never frozen in
5A.

## Walk-forward

Preferred over a single split: calibrate on a past window → evaluate the next period → roll
forward; aggregate out-of-sample performance across folds. Preserves chronology and exposes
regime dependence. Random k-fold is inappropriate (breaks time order and shares
within-day/instrument correlation across folds).

## Day-level dependence

Confidence intervals / bootstraps resample **whole trading days**, not individual
observations. Sample-size claims report the number of independent **days** (and sectors)
alongside raw row counts, so ~thousands of correlated snapshots are never presented as
independent.

## Reporting dimensions (every primary result)

Overall · per sector · by sector-size band (raw `expected_count`, cutoffs TBD in 5C) ·
time-of-day bucket · market-regime stratum (universe proxy sign/magnitude/breadth/dispersion)
· gap regime (continuous `median_overnight_return`, quantile buckets in 5C) · bullish vs
bearish (symmetry). A result that holds only overall (driven by FINANCIAL_SERVICES) but fails
per-sector-band is not accepted.

## Effect size & significance

Report **practical effect size** with uncertainty (e.g. difference in median aligned forward
return; continuation-rate difference; MFE/MAE difference; quantiles) — not p-values alone. A
statistically-significant-but-economically-trivial difference on a large dataset is rejected.
No transaction costs are subtracted from sector labels (sector intelligence is not an
execution strategy); economic meaningfulness of stock selection is a downstream strategy
concern, noted not applied here. No option premium/IV/Greeks/expiry in V1 calibration.

## Multiple testing & overfitting

Predeclare a **small** set of primary outcomes (sector: aligned +15m; stock: aligned forward
+ aligned forward-vs-sector). Everything else is exploratory/secondary. Candidate thresholds
must be derived from **empirical distributions/quantiles**, not brute-forced over hundreds of
arbitrary values. The untouched test set is queried **once**; thresholds are never tuned on
it. Where formal testing is used, apply multiple-comparison awareness.

## Threshold robustness

A candidate threshold X must survive a **neighborhood** check (X−δ, X, X+δ; δ chosen in 5C
from the metric's scale): if the outcome collapses immediately off X, it is rejected as
overfit. Prefer monotone/plateau relationships over knife-edge ones.

## Success criteria (what justifies a production threshold in 5D)

Meaningful forward-outcome separation · adequate independent-day sample · stability across
chronological periods (walk-forward) · reasonable generalization across sectors/size-bands ·
bull/bear symmetry (or explainable asymmetry) · time-of-day stability **or** an explicit
time-dependent rule · robustness to small perturbation · confirmed on the untouched holdout.

## Failure criterion (explicitly allowed)

**NO USEFUL THRESHOLD FOUND** is an acceptable outcome. A metric that shows weak/unstable
forward value stays **diagnostic-only**; it is not forced into a production score or state.

## No ML

V1 uses interpretable statistics (distributions, conditional forward returns, quantiles,
effect sizes). No XGBoost/RandomForest/NN/logistic/clustering without a separate future
decision. Understand the raw signal first.

## Analysis artifacts (not frontend)

5C may emit analytical plots/tables (metric-vs-forward-return quantiles, breadth/RS/percentile
vs continuation, coverage & sector-size vs reliability, time-of-day heatmaps, MFE/MAE
distributions). These are research outputs. **No ApexScan frontend/dashboard/UI is designed in
the SECTOR-5 track**; when a frontend phase is reached, work pauses for the user (who owns
visual design).

## Calibration-run artifact (metadata schema)

`schema_version, run_id, generated_at, source_sha, sector_dataset_version,
sector_dataset_effective_date_policy, universe_methodology, historical_source,
source_data_version_or_hash, date_range, trading_dates_included, trading_dates_excluded,
exclusion_reason_counts, observation_time_schedule, forward_horizons,
calculation_policy_candidates, price_timestamp_semantics, corporate_action_policy,
calendar_version, feature_definitions_version, label_definitions_version, record_counts,
sector_counts, instrument_counts, missing_data_summary, validation_split_definition,
random_seed, tool_version, status`. Features and labels are separate logical datasets
(recommended **Parquet** for schema+efficiency, with CSV/JSON export for inspection; adopt the
Parquet dependency only in 5B and only if justified) joined on
`(trading_date, observation_time, sector_id[, identity])`.

## Data-volume estimate (planning; universe resolved point-in-time, not hard-coded)

≈ constituents (~210 now) × observation_times (~12) × trading_days × (feature cols) plus
sector rows (~18 × 12 × days) and label rows (× horizons). Order-of-magnitude:
- 3 months (~62 trading days): ~210×12×62 ≈ 156k stock-feature rows + ~13k sector rows;
- 6 months ≈ 2×; 12 months ≈ 4×; 24 months ≈ 8× (≈ 1.2M stock-feature rows).
Parquet keeps this comfortably in the low-hundreds-of-MB range — trivial for offline analysis.
**Historical depth:** recommend targeting **≥12 months** (spanning trend/range, high/low vol,
gap days, multiple expiry cycles); **minimum acceptable ~6 months**; anything shorter is
flagged as regime-concentrated and used for development only, not final thresholds.

## Phase contracts

**SECTOR-5B may:** load historical data; resolve point-in-time universe/membership; build
feature observations at T; **reuse** SECTOR-3 metrics & SECTOR-4 ranking; generate forward
labels; write deterministic separated artifacts; run quality + anti-leakage + property tests.
**5B may NOT:** choose thresholds/weights/score; enable live runtime; deploy.

**SECTOR-5C may:** analyze distributions; parameter sweeps (epsilon, min_valid_count,
min_coverage, observation_time); time-of-day / sector-size / regime / gap stratification;
forward-outcome relationships; MFE/MAE; continuation/reversal; feature redundancy &
correlation; threshold-robustness; chronological + walk-forward validation; holdout
evaluation. It **may recommend** candidates. **5C may NOT:** silently deploy.

**SECTOR-5D may (only after 5C evidence + explicit review):** freeze direction_epsilon,
minimum_valid_count, minimum_coverage_ratio, SectorScore + weights, confidence formula +
weights, sector-state thresholds (STRONG_BULLISH…STRONG_BEARISH), stock classification
thresholds (LEADER/PARTICIPANT/LAGGARD/DIVERGENT), small-sector policy, time-of-day rules.
Strength and confidence remain **separate** concepts; correlated components must not receive
independent large weights.
