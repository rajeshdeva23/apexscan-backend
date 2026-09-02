# Data Quality, Exclusions, Leakage & Bias (SECTOR-5A)

## Typed exclusion reasons (records are excluded explicitly, never silently dropped)

`MISSING_PRICE`, `STALE_OR_INCOMPLETE_BAR`, `NO_PREVIOUS_CLOSE`, `CORPORATE_ACTION_UNSAFE`,
`MEMBERSHIP_UNAVAILABLE`, `UNIVERSE_UNAVAILABLE`, `HORIZON_CROSSES_SESSION_END`,
`INSUFFICIENT_CONSTITUENTS`, `INVALID_PRICE`, `CALENDAR_AMBIGUITY`. Every excluded feature row
or label carries its reason; run metadata aggregates counts per reason.

## Quality assertions (SECTOR-5B dataset must satisfy)

- No duplicate feature key `(date, obs_time, sector_id[, identity])`; no duplicate label key.
- All timestamps timezone-aware (UTC canonical); no naive clocks.
- No mixed trading dates within a snapshot; membership effective on the row's date.
- Every feature bar `end_timestamp ≤ observation_time`; every label bar `end_timestamp > observation_time`.
- No future bar present in any feature row (asserted, see leakage tests).
- Return identities consistent: `total ≈ (1+overnight)(1+intraday) − 1` within Decimal tolerance.
- Sector constituent count reconciles with SECTOR-2 membership; coverage = valid/expected.
- `within_sector_rank`/`within_sector_percentile` within valid ranges; percentile `None` for N<2.
- No NaN/Infinity where Decimal semantics forbid; nulls only where the schema declares nullable.

## Anti-leakage tests (SECTOR-5B must implement)

1. Mutating a **future** bar (e.g. 10:00) must **not** change any 09:30 feature row, but
   **must** change the relevant forward labels.
2. Mutating **tomorrow's** data must not change any of today's feature rows.
3. Changing the membership dataset's **future** effective date must not alter past snapshots
   (point-in-time resolution).
4. Feature generation reads no label dataset and no post-T bar (enforced by construction +
   an assertion that the feature pass is given only ≤T bars).

## Property / invariant tests (reuse SECTOR-3/4 guarantees)

Bull/bear symmetry · scale invariance · order invariance · point-in-time membership safety ·
chronological safety · session-boundary safety · corporate-action exclusion determinism ·
missing-data determinism · full **replay determinism** (same source data + SHA + config +
calendar + membership ⇒ byte-identical outputs; no `datetime.now()`, no unordered-iteration
dependence, no network read during compute).

## Bias / risk register

| risk | impact | mitigation | remaining limitation |
|------|--------|-----------|----------------------|
| Look-ahead | inflated/false predictive power | feature/label dataset separation; ≤T-only feature pass; leakage tests | none if tests pass |
| Survivorship | current-universe retrospective over-states reliability | record `universe_methodology`; label runs biased | true point-in-time F&O eligibility not held (5B prereq) |
| Membership leakage | wrong constituents for old dates | point-in-time `resolve_primary(on=date)`; `MEMBERSHIP_UNAVAILABLE` pre-effective | single current dataset snapshot ⇒ limited history |
| Corporate actions | absurd cross-action returns | adjusted daily source; intra-session-only intraday ratios; `CORPORATE_ACTION_UNSAFE` exclusion | depends on a trustworthy adjusted source (5B prereq) |
| Missing/stale bars | distorted metrics | exclusion reasons; coverage tracked; never impute 0 | reduces effective sample |
| Provider timestamp ambiguity | mislabeled grid time | key on `end_timestamp`; empirically confirm bar labeling | must verify in 5B before trusting grid |
| Special/short sessions | invalid horizons | calendar-driven session bounds; `HORIZON_CROSSES_SESSION_END` | rare-session sample sparsity |
| Intraday sample dependence | inflated significance | group by day/sector/instrument; day-level resampling | fewer effective independent units |
| Sector-size imbalance | calibration driven by FINANCIAL_SERVICES | per-sector + size-band reporting | small sectors under-powered |
| Multiple testing | cherry-picked thresholds | predeclared primary outcomes; holdout; stability | many secondary metrics remain exploratory |
| Overfitting | threshold works only on dev data | dev/validation/untouched-test split; robustness neighborhood | test budget finite |
| Regime concentration | period-specific results | span trend/range/vol/gap/expiry; walk-forward | limited by available history depth |
| Bull/bear imbalance | asymmetric bias | symmetry checks; report per-direction | market may be genuinely asymmetric |

## Data-source findings (offline, from repo/provider — no Dhan auth performed)

Provider supports intraday `{1,5,15,25,60}m` (`/charts/intraday`, ≤90-day chunks, ~0.2s min
request spacing) and daily (`/charts/historical`); canonical candles carry explicit
start+end timestamps; engine resamples 1m→N exactly. `HistoricalRequest(instrument,
start_timestamp, end_timestamp, interval)`. **Not provable offline** (SECTOR-5B prerequisites,
require an authenticated fetch under separate authorization): actual 1m depth/coverage for all
constituents, real bar-labeling convention, and a trustworthy corporate-action-adjusted daily
series. Live tick freshness (`freshness_limit`) is **not** fully calibratable from candle
replay — historical *data completeness* ≠ live *event freshness*; freshness_limit calibration
needs later shadow/live evidence (documented, not faked).
