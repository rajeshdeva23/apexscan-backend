# ADR-007 Addendum — Previous Session Relative Range Strategy Specification (V1)

| Field | Value |
|-------|-------|
| **Type** | Subordinate strategy specification (not a numbered ADR) |
| **Subordinate to** | ADR-007 (dynamic strategy lifecycle & requirement management) |
| **Related** | ADR-007 multi-session historical lookback capability (MSH1-16), Narrow CPR / PSR / PSB specifications (templates), partial-universe addendum, ADR-011 (calendar), ADR-012 (scanner + REST), ADR-013 (registration), MULTI-SESSION-STRATEGY-SELECTION-GOV-R1 |
| **Status** | Accepted (specification) — **implementation DEFERRED** to PREVIOUS-SESSION-RELATIVE-RANGE-IMPL-R1 |
| **Date** | 2026-08-20 |
| **Deciders** | Strategy / Platform Architecture |
| **Decision** | Govern the first **multi-session** production scanner — **Previous Session Relative Range** — ranking NSE cash-equity instruments by how compressed the previous completed session (D-1) was relative to a fixed 20-session normalized-range baseline. Basis-safe (per-session dimensionless ratios), completed-session-only, non-directional, reuses the validated multi-session capability + generic scanner/REST unchanged. |

---

## Context

Selected by MULTI-SESSION-STRATEGY-SELECTION-GOV-R1 (OUTCOME A) as the first strategy to consume the validated `HistoricalRequirement(Timeframe.session(), lookback=N>1)` capability (MULTI-SESSION-HISTORICAL-LOOKBACK-VALIDATION-R1). It adds a **self-relative** compression dimension orthogonal to the three frozen **absolute** metrics. `HistoricalContext.series` delivers exactly N completed sessions oldest→newest (`series[-1]=D-1`), calendar-aware, current-day excluded. The strategy-import guard forbids cross-implementation imports, so `range_pct` is re-implemented in this package.

## Decisions PSRR1–PSRR30

- **PSRR1 — Strategy identity.** `strategy_id = "previous_session_relative_range"`; display "Previous Session Relative Range"; version `1.0.0`; category `StrategyCategory.MARKET_STRUCTURE`; emission `ONE_SHOT_PER_SESSION`.
- **PSRR2 — Authoritative inputs.** Only completed-session `Candle` open/high/low from `HistoricalContext.series`. No current-session data, close-based cross-session comparison, volume, or SessionStatistics.
- **PSRR3 — Range-percent formula.** Per session i: `range_pct_i = (high_i − low_i) / open_i × 100`. Re-implemented in this package's calculator (not imported from PSR).
- **PSRR4 — Decimal policy.** `Decimal` only, under a fixed `localcontext(prec=28)`; no float; no quantize/round before ranking; no display rounding in backend.
- **PSRR5 — Baseline-session count.** **FIXED `baseline_sessions = 20`** (a module constant). Rationale: `Strategy.requirements` is a *static* property (decoupled from the configuration instance, per the frozen strategies), so a configurable baseline could not flow into the declared `HistoricalRequirement` — making a configurable lookback unsafe/ambiguous (STOP #17). V1 fixes it; the configuration exposes **no** `baseline_sessions` field.
- **PSRR6 — Historical lookback.** Static `HistoricalRequirement(Timeframe.session(), lookback = 21)` (`baseline_sessions + 1`).
- **PSRR7 — Series partition.** Require `series` for the session timeframe with **exactly 21** candles, oldest→newest. `subject_candle = series.candles[-1]` (D-1); `baseline_candles = series.candles[:-1]` (exactly 20 = D-2…D-21). D-1 is **excluded** from its own baseline. No strategy-side sorting; no calendar/date arithmetic in the strategy.
- **PSRR8 — Median algorithm (exact).** Over the 20 baseline `range_pct` `Decimal` values: copy → sort ascending → since N=20 is even, `lower = sorted[9]`, `upper = sorted[10]`, `baseline_range_pct = (lower + upper) / Decimal("2")`, under `localcontext(prec=28)`. No `statistics.median`, no float, no percentile/interpolation library, no pre-median quantization. (A generic odd-N branch — middle element — is documented but V1 is fixed at even N=20.)
- **PSRR9 — Relative-range formula.** `relative_range_ratio = range_pct(subject_candle) / baseline_range_pct`. No pre-ratio quantization, no inversion (`1/ratio`), no score transform.
- **PSRR10 — Zero/degenerate semantics.** (A) `previous_range_pct == 0`, `baseline_range_pct > 0` → **VALID**, `ratio = 0` (maximally compressed; participates in ranking, rank 1). (B) `baseline_range_pct == 0` → **SKIPPED**, reason `PREVIOUS_SESSION_RELATIVE_RANGE_DEGENERATE_BASELINE`; never divide-by-zero / fabricate 0 or 1 / epsilon / drop-zero-sessions / substitute mean. (C) individual baseline sessions with `range_pct == 0` but median `> 0` → **VALID** (zeros are legitimate baseline members; only the final median==0 is degenerate).
- **PSRR11 — Configuration.** `PreviousSessionRelativeRangeConfiguration` carries only `config_version`. No `baseline_sessions`, threshold, direction, or weight field (PSRR5).
- **PSRR12 — FactNeed.** `FactNeed.PREVIOUS_SESSION`. The subject is read from `series[-1]` (single authoritative computational path); `previous_session` (== `series[-1]`, populated alongside the series) supplies the authoritative `source_session_date`. **Not** `FactNeed.SESSION_STATISTICS`.
- **PSRR13 — Trigger.** `StrategyTrigger.ON_HISTORICAL_READY`.
- **PSRR14 — Emission policy.** `EmissionPolicy.ONE_SHOT_PER_SESSION` (all inputs immutable during D).
- **PSRR15 — Result metrics.** Named `MetricEntry` tuple: `relative_range_ratio` (Decimal), `previous_range_pct` (Decimal), `baseline_range_pct` (Decimal), `baseline_sessions` (int = 20), `source_session_date` (str, ISO, from `previous_session.trading_date`). **No** baseline array / raw OHLC / provider ids / direction / score / probability exposed.
- **PSRR16 — Score policy.** `None`.
- **PSRR17 — Ranking.** `ScannerRankingPolicy(strategy_id="previous_session_relative_range", metric_name="relative_range_ratio", ordering=ASCENDING)`. No ranking/sort in the strategy; no `strategy_id` branch in the scanner.
- **PSRR18 — Tie-break.** Canonical generic `(exchange, symbol)` ascending; no strategy-specific secondary key.
- **PSRR19 — Missing history.** Fewer than exactly 21 authoritative completed sessions → **SKIPPED**, reason `PREVIOUS_SESSION_RELATIVE_RANGE_NO_HISTORY`; never a shorter/variable baseline, fabricated/duplicate/zero candle, or substituted instrument. Provider M<21 = local instrument gap → PARTIAL; insufficient calendar coverage for the 21-session window = GLOBAL fail-closed (unchanged from the validated capability).
- **PSRR20 — Partial universe.** RUNNING + PARTIAL for local gaps; `expected_count` = universe size; missing instruments absent; no placeholder/fabrication; no global ERROR for local gaps (ADR-007 partial-universe model).
- **PSRR21 — Price-basis proof.** For any session scaled uniformly by factor `F` (`H'=FH, L'=FL, O'=FO`): `(H'−L')/O' = (FH−FL)/(FO) = (H−L)/O`. Every per-session `range_pct` is invariant to a uniform per-session corporate-action factor; the median operates on dimensionless %s; the final ratio compares dimensionless quantities. Therefore **BASIS-SAFE** — no cross-session raw-price comparability required. This does **not** generalize to raw close-to-close return / raw high-low or close comparisons (still ungoverned).
- **PSRR22 — No-look-ahead.** Reads only completed sessions ≤ D-1; changing today's tick/price/SessionStatistics with identical 21-session input cannot change the result. `NO_LOOK_AHEAD = YES`.
- **PSRR23 — Non-repainting.** Fully determined before D opens; immutable during today. `NON_REPAINTING = YES`.
- **PSRR24 — Provider neutrality.** Only canonical `Candle`/`Instrument`/`Timeframe`/`MarketContext`; no Dhan/security_id/exchange_segment/httpx/websocket/redis/sqlalchemy. Architecture import-boundary guard stays green.
- **PSRR25 — Scanner reuse.** Generic `CrossInstrumentStrategyScanner` unchanged; no branch; no strategy-side sort.
- **PSRR26 — REST reuse.** Generic `GET /api/v1/scanners/previous_session_relative_range`; `relative_range_ratio` transported as an exact Decimal **string**; `?limit=N` truncates candidates only, counts intact; no PSRR-specific schema.
- **PSRR27 — Disabled-demand.** Not in `strategies_enabled` ⇒ zero historical/runtime demand (no auto-discovery/scheduler/task/warmup). When enabled beside the frozen strategies, the session requirement union widens to `max(1,1,1,21)=21` (one shared warmup).
- **PSRR28 — Authority isolation.** Requires none of `staged_observation_verified`/`tick_aggregate_verified`/`supports_current_day` (all remain False); no SessionStatistics; unrelated to Open=High/Low.
- **PSRR29 — Task topology.** `NEW create_task SITES = 0`; `market_runtime.py` count stays 3.
- **PSRR30 — Frozen-strategy isolation.** Narrow CPR / PSR / PSB unchanged (formulas/config/descriptors/requirements/ranking/reason-codes/calculators/results/REST/frontend/tests). New strategy re-implements `range_pct` independently.

## Readiness proof (why 20 / partial / previous_session-only cannot satisfy)

Readiness requires the session `HistoricalRequirement(lookback=21)` to be **SATISFIED**. VALIDATION-R1 proved warmup only SATISFIES a session timeframe when the full N candles are present (`_direct_series` returns `None` for M<N → unresolved → not ready); a 20-candle or partial series is unresolved; `previous_session` alone does not satisfy the 21-session requirement. `evaluate` additionally guards defensively: absent session series or `len != 21` → SKIPPED (`PREVIOUS_SESSION_RELATIVE_RANGE_NO_HISTORY`).

## Future implementation shape (deferred)

`app/strategies/implementations/previous_session_relative_range/{__init__,calculator,configuration,strategy}.py` + one `production_catalog` entry with the ASCENDING `ScannerRankingPolicy` + calculator/strategy/registration/E2E tests. Frontend is a separate, later, user-designed slice.

## Consequences

- A basis-safe, non-directional, self-relative compression scanner ships on the frozen platform + validated multi-session capability with zero generic/backend-infra change and zero new tasks.
- The three V1 strategies stay frozen.
- Implementation deferred to PREVIOUS-SESSION-RELATIVE-RANGE-IMPL-R1.
