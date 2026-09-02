# Feature Schema — data known at observation time T (SECTOR-5A)

All features are produced by the **merged SECTOR-3/4 pure functions** at T (no re-implementation)
and use only data with timestamp ≤ T. Returns are Decimal ratios (`0.01` == 1%). Nothing here
is a score/threshold. Join keys: `(trading_date, observation_time, sector_id[, identity])`.

## Universe features (one row per `trading_date × observation_time`)

| name | source | formula | nullable | purpose |
|------|--------|---------|----------|---------|
| universe_valid_count | `calculate_universe_proxy` | # valid+fresh eligible F&O obs | no | regime / support |
| universe_proxy_intraday_return | `calculate_universe_proxy` | equal-weight median intraday over valid F&O | yes (0 valid) | market regime, relative-strength base |
| universe_advance_ratio / decline_ratio | derived (same obs set) | breadth over all valid F&O | yes | broad-market breadth regime |
| universe_dispersion_mad | statistics.mad over F&O intraday | `median(|x−median|)` | yes (<1) | cross-market dispersion regime |

## Sector features (one row per `trading_date × observation_time × sector_id`) — from `SectorMetrics`

| name | source | formula / note | nullable | purpose |
|------|--------|----------------|----------|---------|
| expected_count | SECTOR-2 membership @date | size of primary sector | no | sector-size band, coverage denom |
| valid_count / invalid_count / stale_count / missing_count | SECTOR-3 | counts | no | support, coverage calibration |
| coverage_ratio | SECTOR-3 | valid/expected | no | coverage calibration (§ vs count) |
| advance_count / decline_count / unchanged_count | SECTOR-3 | epsilon-gated | no | breadth-structure analysis |
| advance_ratio / decline_ratio / unchanged_ratio | SECTOR-3 | count/valid | yes (valid 0) | breadth analysis |
| net_breadth | SECTOR-3 | (adv−dec)/valid ∈[−1,1] | yes | breadth calibration |
| median_overnight_return | SECTOR-3 | median gap | yes | gap stratification |
| median_intraday_return | SECTOR-3 | median since-open | yes | **core strength**, relative-strength base |
| median_total_return | SECTOR-3 | median vs prev close | yes | diagnostic |
| mad_intraday_return | SECTOR-3 | MAD | yes (valid 0) | dispersion calibration |
| iqr_intraday_return | SECTOR-3 | Tukey exclusive hinges | yes (valid<2) | dispersion calibration |
| directional_agreement | SECTOR-3 | median-tilt ratio | yes | agreement value (Q6) |
| directional_participant_count / ratio | SECTOR-3 | median-direction participants | yes | participation value (Q7) |
| raw_sector_direction | SECTOR-3 | BULLISH/BEARISH/NEUTRAL/MIXED/INSUFFICIENT | no | strata + direction-normalisation |
| universe_proxy_intraday_return | SECTOR-3 (supplied) | see universe table | yes | relative-strength base |
| sector_relative_strength | SECTOR-3 | median_intraday − universe_proxy | yes | relative-strength calibration |
| overnight_return_median (regime) | SECTOR-3 | = median_overnight_return | yes | gap regime |
| policy_direction_epsilon / min_valid_count / min_coverage_ratio | run config | candidate params echoed | no | metadata (sweep) |
| **(analysis-only)** ex_sector_universe_proxy | offline recompute | universe median **excluding** this sector's members | yes | Q20 whole vs ex-sector (analysis only; **not** a production change) |

## Stock features (one row per eligible constituent) — from `StockSectorMetrics`

| name | source | formula | nullable | purpose |
|------|--------|---------|----------|---------|
| stock_intraday_return | SECTOR-4/3 | (ltp−open)/open | no (eligible) | absolute behaviour (Q11/Q12) |
| stock_overnight_return / stock_total_return | SECTOR-4/3 | decomposition | no | gap context |
| constituent_direction | SECTOR-4/3 | ADVANCING/DECLINING/UNCHANGED | no | alignment |
| stock_vs_sector | SECTOR-4 | intraday − sector_median | yes (median None) | Q12 |
| stock_vs_universe | SECTOR-4 | intraday − universe_proxy | yes (proxy None) | Q42 incremental info |
| robust_relative_magnitude | SECTOR-4 | (intraday−median)/MAD, None if MAD 0/None | yes | Q13 cross-sector comparability |
| sector_alignment | SECTOR-4 | ALIGNED/OPPOSED/NEUTRAL/MIXED_CONTEXT/INSUFFICIENT_DATA | no | Q14 OPPOSED analysis |
| directional_strength | SECTOR-4 | sector-signed intraday | yes (non-directional sector) | rank basis |
| within_sector_rank | SECTOR-4 | competition rank | yes | Q11 |
| within_sector_percentile | SECTOR-4 | (N−rank)/(N−1), None if N<2 | yes | Q11 (percentile calibration) |
| eligible / exclusion_reason | SECTOR-4 | bool / STALE | no / yes | eligibility audit |

## Metadata retained per feature row (reproducibility)

`schema_version`, `run_id`, `source_sha`, `sector_dataset_version`, `universe_methodology`,
`calendar_version`, `price_timestamp_semantics`, `observation_time`, `feature_definitions_version`.
No `SectorScore`, no `confidence`, no state/label columns live in the feature datasets.
