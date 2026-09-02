# ADR-016 — Sector Metrics Definitions (SECTOR-3, subordinate)

Subordinate to **ADR-016**. Records the exact mathematical definitions implemented by the
pure metrics engine (`app/market_intelligence/sector/metrics/`). It does not alter any
ADR-016 decision. **Every threshold here is UN-CALIBRATED**: `direction_epsilon`,
`freshness_limit`, `minimum_valid_count`, and `minimum_coverage_ratio` are explicit inputs
supplied by the caller (`CalculationPolicy`), never production values. SECTOR-5 calibrates.

## Numeric representation

All prices and returns are `Decimal`. Returns are **ratios** (`0.01` == 1%), never
percentages, computed under the caller's active decimal context (28 significant digits by
default) — deterministic across environments. Ratios that are mathematically unavailable
are `None`, never fabricated (e.g. `0`).

## Return decomposition (per constituent)

- `overnight_return = (session_open − previous_close) / previous_close`
- `intraday_return = (last_price − session_open) / session_open`
- `total_return = (last_price − previous_close) / previous_close`

Intraday (since-open) movement drives direction; overnight movement is **context only**
(SECTOR-1). A gap-up that sells off from the open is intraday-bearish; a gap-down that
recovers is intraday-bullish.

## Direction (`direction_epsilon`)

`intraday_return > +ε` → `ADVANCING`; `< −ε` → `DECLINING`; else `UNCHANGED`. The same ε
defines the neutral band for the sector median tilt.

## Breadth (equal-weight; no market-cap weighting)

Over `N` valid (fresh) constituents: `advance_count`, `decline_count`, `unchanged_count`;
`*_ratio = count / N`; `net_breadth = (advance_count − decline_count) / N ∈ [−1, +1]`.
Counts sum to `N` exactly; ratios sum to 1 up to decimal precision. `None` when `N = 0`.

## Central tendency

`median_intraday/overnight/total_return`. Median: odd → middle ordered value; even → mean
of the two middle values. Median (not mean) is the directional authority (outlier/
heavyweight resistance). `None` when `N = 0`.

## Dispersion

- `MAD = median(|x_i − median(x)|)`; `0` for a single value; `None` when empty.
- `IQR = Q3 − Q1` using **Tukey exclusive hinges**: split the sorted sample at the median,
  dropping the middle element when `N` is odd; `Q1`/`Q3` are the medians of the lower/upper
  halves. `None` when `N < 2` (no statistical spread from a single point).

## Directional agreement

Relative to the median tilt: bullish → `advance_ratio`; bearish → `decline_ratio`; neutral
→ `unchanged_ratio`. Symmetric; `None` when `N = 0`.

## Participation

`directional_participant_count` = valid constituents moving in the median-tilt direction
beyond ε (bullish → `intraday > +ε`; bearish → `intraday < −ε`; neutral → 0).
`directional_participation_ratio = count / N`. Component counts remain separately visible
(breadth), so multiplication never hides information.

## Raw direction (un-calibrated; NOT the SECTOR-5 strong/weak classification)

`INSUFFICIENT_DATA` if `valid_count == 0`, `valid_count < minimum_valid_count`, or (when
supplied) `coverage_ratio < minimum_coverage_ratio`. Otherwise a directional call requires
median-tilt and breadth-tilt (sign of `net_breadth`) to **agree**: both bull → `BULLISH`;
both bear → `BEARISH`; both neutral → `NEUTRAL`; any disagreement (including
neutral-vs-directional) → `MIXED`. This prevents a single heavyweight from manufacturing
`BULLISH`.

## Universe proxy & relative strength

`calculate_universe_proxy` = equal-weight median intraday return over **all** valid, fresh
eligible F&O observations (whole-universe median, **including** the sector — chosen for V1
simplicity/stability; ex-sector benchmarking not justified at V1 scale). It is an
F&O-universe proxy, **not** a NIFTY index return.
`relative_strength = sector_median_intraday − universe_proxy_median_intraday` (raw; no
outperforming/inline/underperforming threshold states). `None` if either operand is `None`.

## Coverage & freshness

`expected_count` = SECTOR-2 primary membership size for the sector. `coverage_ratio =
valid_count / expected_count`. An observation is **fresh** iff
`0 ≤ (evaluation_time − observation_timestamp) ≤ freshness_limit` (timezone-aware; boundary
inclusive). Stale observations **never** contribute to any metric but still lower coverage.
Missing constituents lower coverage and are **never** treated as `UNCHANGED`. Coverage is
not confidence — confidence is SECTOR-5.

## Fail-closed invariants

Non-positive prices are rejected at `ConstituentObservation` construction; duplicate
identities, mixed trading dates, and membership mismatches raise typed
`SectorMetricsError` subclasses. Same input → same output; results are order-, scale-, and
sign-symmetric (`f(−x)` mirrors `f(x)` for breadth/median; MAD/IQR/participation invariant
in magnitude).
