# ADR-016 — Stock Participation & Ranking Definitions (SECTOR-4, subordinate)

Subordinate to **ADR-016**. Records the mathematical definitions of the pure stock-level
layer (`app/market_intelligence/sector/participation/`) built on SECTOR-2 membership and
SECTOR-3 metrics. It changes no ADR-016 decision. **Everything here is RAW / UN-CALIBRATED**:
there is no StockParticipationScore, no LEADER/PARTICIPANT/LAGGARD/DIVERGENT threshold, no
confidence. `within_sector_rank == 1` does **not** mean a production "LEADER" — SECTOR-5
calibrates all boundaries.

## Inputs

`rank_sector_constituents(sector_metrics, observations, policy)` where `sector_metrics` is a
SECTOR-3 `SectorMetrics` (supplies `sector_id`, `trading_date`, `evaluation_timestamp`,
`median_intraday_return`, `universe_proxy_intraday_return`, `raw_direction`,
`dispersion.mad_intraday_return`). Return math is reused from SECTOR-3
(`calculate_constituent_metrics`) — not re-implemented. Returns are Decimal ratios.

## Three independent facts (never collapsed)

- **absolute**: `stock_intraday_return` (since open)
- **stock_vs_sector** = `stock_intraday_return − sector_median_intraday_return`
- **stock_vs_universe** = `stock_intraday_return − universe_proxy_intraday_return`

Either relative value is `None` when its benchmark is `None`. A stock can be absolutely
positive, sector-lagging, and universe-outperforming simultaneously — all three remain visible.

## Sector alignment

Relative to `sector_metrics.raw_direction`: BULLISH+advancing / BEARISH+declining → `ALIGNED`;
opposite → `OPPOSED`; stock UNCHANGED or sector NEUTRAL → `NEUTRAL`; sector MIXED →
`MIXED_CONTEXT`; sector INSUFFICIENT_DATA → `INSUFFICIENT_DATA`. No alignment is asserted when
sector evidence is mixed/insufficient.

## Directional strength & ranking

`directional_strength` = `+intraday` (BULLISH sector) / `−intraday` (BEARISH sector) / `None`
otherwise — larger means stronger movement in the sector's direction (bull/bear symmetric).
Ranking is produced **only** when the sector raw direction is BULLISH/BEARISH
(`directional_ranking_available`); otherwise eligible stocks carry absolute/relative metrics
but `within_sector_rank`/`within_sector_percentile`/`directional_strength` are `None` and are
ordered by identity.

- **within_sector_rank**: competition ranking on `directional_strength` (ties share a rank;
  next rank skips). Rank 1 = strongest in sector direction.
- **display order**: `directional_strength` descending, then canonical identity ascending
  (identity is a display-only tiebreaker — it never implies different market strength).
- **within_sector_percentile**: `(N − rank) / (N − 1)` ∈ [0,1] (1 = strongest); `None` for
  `N < 2` (a lone constituent carries no relative superiority).

## Robust relative magnitude (diagnostic)

`(stock_intraday_return − sector_median) / sector_MAD`, or `None` when the median or MAD is
unavailable or MAD is 0 (never divide by zero; no scaling constant such as 0.6745). Diagnostic
only — not a score.

## Eligibility, coverage, fail-closed

Freshness uses `sector_metrics.evaluation_timestamp` (single source): fresh iff
`0 ≤ eval − observation_timestamp ≤ freshness_limit`. Stale observations are **excluded**
(`eligible=False`, `exclusion_reason=STALE`) — never ranked, never treated as unchanged.
Missing constituents are simply absent (reflected in SECTOR-3 coverage), never fabricated.
Fail-closed on duplicate identity (`DuplicateRankedConstituentError`) and on sector/trading-date
mismatch vs the `SectorMetrics` context (`StockSectorContextMismatchError`).

## Invariants

Pure/deterministic (no I/O, DB, Redis, EventBus, provider, wall-clock, global state); order-,
scale-, and sign-symmetric (bull/bear mirror produces mirrored ranks/percentiles/alignment and
negated relative values); outliers rank correctly without altering the SECTOR-3 median (the
median is consumed, never recomputed here). N=1 works without any confidence claim (TEXTILES
remains a SECTOR-5 calibration concern).
