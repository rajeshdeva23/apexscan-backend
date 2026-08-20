# ADR-007 Subordinate Artifact — Narrow CPR Strategy Specification (V1)

| Field | Value |
|-------|-------|
| **Type** | Subordinate strategy specification (not a numbered ADR) |
| **Subordinate to** | ADR-007 — Dynamic Strategy Lifecycle & Requirement Management (D15: per-strategy rules are individual specifications authored separately) |
| **Related** | ADR-004 (208 NSE cash-equity scanner universe), ADR-006 (candle completeness), ADR-010 (runtime/task lifecycle), ADR-011 + addenda (authoritative trading-calendar / previous-session authority), `docs/07_STRATEGY_ENGINE.md` (§12 result, §13 scoring, §14 ranking) |
| **Status** | Accepted (mathematical + data + no-look-ahead + result + ranking-key contract); **implementation DEFERRED** to NARROW-CPR-IMPL-R1 |
| **Date** | 2026-08-16 |
| **Deciders** | Strategy / Market-Engine Architecture |
| **Scope** | Freezes the Narrow CPR **V1** contract. Does NOT implement code, add a strategy, change execution, activate trading, change authority flags, or weaken historical fail-closed behaviour. Does NOT restate ADR-007 framework decisions (D15) and introduces NO CPR math into the Market Engine (docs/06 §228; glossary §626). |

> **Numbering note.** Per ADR-007 D15 and the ADR README (strategy specs are separate/subordinate artifacts, not numbered ADRs), this is a subordinate specification to ADR-007. If repository owners later prefer a numbered ADR-012, this content transfers verbatim.

---

## Context & preflight (from repository inspection)

- **Previous-session OHLC is a governed, first-class path.** Declaring `HistoricalRequirement(Timeframe.session(), lookback=1)` (`app/market_engine/historical/requirements.py`) drives `HistoricalWarmupService.warmup` over the authoritative calendar and installs a `HistoricalContext` into the shared `InstrumentStateRegistry`; the strategy reads it at `evaluate` time via `context.historical.previous_session` → `PreviousSessionFacts(trading_date, candle)` and `context.historical.series` (`app/market_engine/historical/context.py`). **Subtlety:** `FactNeed.PREVIOUS_SESSION` alone does **not** trigger warmup — the session `HistoricalRequirement` is the mandatory causal declaration; the fact-need is only an additional readiness assertion.
- **Completed-sessions-only, fail-closed calendar.** `HistoricalRangePlanner`/`HistoricalCalendarWindow` resolve over `previous_trading_days` (weekends/holidays skipped, exceptional-OPEN included, outside `CalendarCoverage` → `OutsideCalendarCoverageError`), never the current possibly-incomplete session; `supports_current_day=False` is hard-coded in the warmup wiring.
- **Completeness is authoritative-by-construction.** `HistoricalContext` carries only authoritative candles; an incomplete session is simply absent → the readiness gate under-fills → the strategy stays not-ready (never a fabricated result).
- **Session OHLC is already canonically aggregated** across one or many live intervals (`resampling.py`: first-interval open, max high, min low, last-interval close, closed gaps excluded).
- **Result/ranking framework exists and supports a non-directional feature.** `StrategyEvaluation(status ∈ {MATCHED, NO_MATCH, SKIPPED, ERROR}, score: Decimal|None, reason_codes, metrics: MetricEntry[value: Decimal|int|str|bool])` → promoted to `StrategyResult` → `rank_results` (per-instrument, across strategies, descending score / ascending strategy_id). **No directional/bias field exists anywhere.** The **cross-instrument** scanner ranking surface does **not** exist yet.
- **CPR math is greenfield** — no CPR/pivot/compression/percentile governance or code exists anywhere. `app.strategies.implementations.narrow_cpr` is reserved only by an import-boundary test.
- **Historical price basis is undocumented** (no adjustment flag sent to Dhan; raw-vs-adjusted unspecified).

**No STOP condition is triggered** (all 10 assessed in the phase report). This spec proceeds.

---

## Decisions NCR1–NCR23

- **NCR1 — Authoritative input session.** Narrow CPR for trading date `T` uses **only** the previous **completed authoritative** trading session `D-1`, obtained from `context.historical.previous_session` (`.trading_date`, `.candle`). Never `calendar_date − 1`; never the current day; never a settings/monitor/synthetic source.

- **NCR2 — CPR formulas.** From the previous session's `H = candle.high_price`, `L = candle.low_price`, `C = candle.close_price`:
  `Pivot P = (H + L + C) / 3`; `BC = (H + L) / 2`; `TC = 2·P − BC`.

- **NCR3 — BC/TC normalization.** `cpr_bottom = min(BC, TC)`, `cpr_top = max(BC, TC)`, `cpr_width = cpr_top − cpr_bottom = |TC − BC|`. Raw `pivot`, `bc`, `tc` are also emitted for transparency. Rationale: `TC`/`BC` invert with previous-session geometry; width must be orientation-independent.

- **NCR4 — Numeric representation / precision.** All arithmetic in `Decimal` (`Candle` prices are `Decimal(gt=0)`). **Internal calculations remain unrounded** (exact Decimal). Rounding occurs **only at emission** (display quantization of price-level metrics and of `cpr_width_pct`). Ranking/comparison use the **unrounded** `cpr_width_pct`. No TradingView-specific rounding is inherited. Division-by-zero is impossible: `H, L, C > 0 ⇒ P > 0`.

- **NCR5 — Primary narrowness metric (V1).** **Pivot-normalized CPR width percentage:** `cpr_width_pct = (cpr_top − cpr_bottom) / P × 100` (Decimal; **smaller = narrower**). Cross-stock comparable (dimensionless % of price). Chosen over: (B) previous-close-normalized — pivot is the CPR's intrinsic center; (C) previous-range-normalized `(TOP−BOTTOM)/(H−L)` — unstable when the previous range is tiny and conflates a different concept; (D) historical-relative — deferred (NCR6).

- **NCR6 — Historical-relative narrowness: NOT in V1.** Deferred to V2. Reasons: (1) the historical price basis is undocumented (NCR22), so comparing widths across a multi-session window risks split/bonus distortion; (2) higher warmup/data cost; (3) V1 must be explainable and low-risk. V1 uses absolute normalized width only (Option A of the phase brief).

- **NCR7 — Historical window N.** **Not applicable to V1** (single previous session; `lookback = 1`). **Deferred V2 shape:** request `N+1` completed sessions (`Timeframe.session()`, `lookback = N+1`); the newest (`D-1`) is the current CPR basis, the `N` older (`D-2 … D-(N+1)`) form the reference width distribution. Suggested starting `N = 20` (≈ one trading month) — re-decided at V2, not fixed here.

- **NCR8 — No-look-ahead boundary.** Today's CPR uses **only** information from completed sessions strictly before `T`. Enforced structurally by: (a) the planner resolving over completed sessions only (`newest_completed_session`/`previous_trading_day`); (b) `supports_current_day=False`; (c) `evaluate` being pure and reading **only** `context.historical` — never `latest_tick`/`latest_quote`/`latest_candle`/`candle_sets`/`session_statistics`/live volume/OI/VWAP/partial candles. No current-day datum may influence CPR geometry.

- **NCR9 — Previous-trading-session semantics.** "Previous session" = previous completed **authoritative** trading session via the `TradingCalendar` (weekends/holidays skipped, exceptional-OPEN included, outside-coverage fail-closed). The strategy **must not** re-implement holiday/weekend logic; it consumes the planner-resolved `previous_session` and session series.

- **NCR10 — Result model.** Reuse the existing `StrategyEvaluation` (no new type; docs/07 §12). On a valid CPR: `status = MATCHED`, ≥1 `reason_code` (e.g., `NARROW_CPR_COMPUTED`; add `NARROW_CPR_THRESHOLD_MET` when a configured threshold is met). `metrics` (Decimal `MetricEntry`s) carry: `cpr_width_pct` (authoritative), `pivot`, `bc`, `tc`, `cpr_top`, `cpr_bottom`, `cpr_width`, `previous_high`, `previous_low`, `previous_close`. `source_session_date` (= `previous_session.trading_date`) is emitted in `metadata`/`metrics` for auditability. **No new classification enum** (VERY_NARROW/NARROW/…) is invented — the continuous `cpr_width_pct` is cleaner (a threshold-derived classification MAY be added later, not V1).

- **NCR11 — Filter vs ranking.** V1 is a **continuous ranking feature**, not a hard boolean filter by default. Every instrument with a valid CPR → `MATCHED` + metrics. **Ranking key = `cpr_width_pct`, ASCENDING (narrowest = rank 1), tie-break ascending instrument symbol** (deterministic; §14). Optional config `narrow_cpr_max_width_pct: Decimal | None = None`: when set, `width_pct > threshold ⇒ NO_MATCH` (filtered), else `MATCHED`. Because the platform `rank_results` ranks **descending by score** and is a **per-instrument-across-strategies** surface (not the cross-instrument scanner), V1 leaves `StrategyEvaluation.score = None`; the authoritative narrowness key is the emitted `cpr_width_pct` metric. The **cross-instrument scanner ranking surface does not exist** and its construction is **deferred** to a separate scanner-surface slice (NCR23) — this spec governs the key + tie-break, not the surface.

- **NCR12 — Directional-bias prohibition.** Narrow CPR is a **non-directional compression/context feature**. It MUST NOT emit BUY/SELL/bullish/bearish (the platform has no directional field, and none is added). The product-overview "trending-day" hint is a downstream/human interpretation, **not** a signal from this strategy. Direction is a separate future feature (Open=High/Open=Low/momentum), not coupled here.

- **NCR13 — HistoricalRequirement declaration.** `StrategyRequirements(historical=(HistoricalRequirement(Timeframe.session(), lookback=1),), fact_needs=(FactNeed.PREVIOUS_SESSION,), trigger=StrategyTrigger.ON_HISTORICAL_READY, candle_completeness=CandleCompleteness.AUTHORITATIVE_ONLY, …)`. The session `HistoricalRequirement(lookback=1)` is the **mandatory causal** declaration (it drives warmup and populates `previous_session`); `FactNeed.PREVIOUS_SESSION` is an explicit readiness assertion. `EmissionPolicy.ONE_SHOT_PER_SESSION` (CPR is stable all day). `StrategyCategory.MARKET_STRUCTURE` (compression/structure context; `VOLATILITY` is an acceptable alternative).

- **NCR14 — Failure / insufficient-history semantics.** Reuse existing contracts; **never** substitute `calendar−1`, partial current-day data, `settings.nse_holidays`, the Dhan monitor, synthetic OHLC, or zeroes. Missing/insufficient previous session → the warmup readiness gate (`Readiness.MISSING_HISTORICAL`) holds the strategy not-ready → it does not evaluate/emit (fail-closed). Outside-coverage / warmup-unavailable / provider-failure are the existing **distinct** errors at the warmup layer → readiness unsatisfied → no evaluation. If the strategy is READY but the previous-session `Candle` is defensively malformed (should not occur — authoritative-by-construction): return `EvaluationStatus.SKIPPED` with a reason code (e.g., `MALFORMED_PREVIOUS_SESSION`), never a `MATCHED`/score. **No new `NOT_EVALUATED`/`INSUFFICIENT_HISTORY`/`FAILED` status is invented** (readiness gating + `SKIPPED` already represent these).

- **NCR15 — Multi-interval transparency.** The previous-session `Candle` is already aggregated across one or many live intervals (first open / max high / min low / last close, gaps excluded). Narrow CPR consumes the canonical session `Candle` and is **interval-count agnostic**; it contains no multi-interval logic.

- **NCR16 — Zero-demand / lazy behaviour.** Demand-driven, preserved: a disabled/unregistered Narrow CPR declares zero requirements ⇒ zero historical demand ⇒ zero provider calls. Only a **STARTED** Narrow CPR adds its session `HistoricalRequirement` to the effective union that `RequirementsCoordinator.warm(...)` fetches. No eager startup download.

- **NCR17 — Scanner scaling / concurrency ownership.** ADR-010 remains authoritative: **no per-instrument/per-stock tasks**, no per-stock scheduler. Warmup over the 208-instrument universe uses the existing single warmup path; fetches are deduped/coalesced by `HistoricalCache` + `HistoricalCoordinator` (bounded `asyncio.Semaphore`), keyed by `(instrument, timeframe, window)`. `evaluate` is pure per-instrument per `MarketContext`. The cross-instrument aggregation/ranking surface (NCR23) is deferred and must also obey ADR-010 (no unmanaged tasks); deterministic ranking runs after candidates resolve.

- **NCR18 — Configuration ownership.** Mathematical definitions are **code/domain contracts**, not Settings (CPR formula, normalization, `cpr_width_pct` are fixed; no magic literals). The only V1 tunable is optional `narrow_cpr_max_width_pct: Decimal | None = None` on the strategy's `StrategyConfiguration`. V2 adds `narrow_cpr_history_sessions: int`. Enablement is a strategy **registration/lifecycle** concern (whether the strategy is started), not a new global Settings flag. No configuration explosion.

- **NCR19 — Determinism / replay.** Identical `{calendar dataset version, historical OHLC, strategy configuration, CPR-spec version}` ⇒ identical output. `evaluate` is pure: no wall clock (uses manager-supplied `StrategyEvaluationMetadata.observed_at`/`trading_date`), no network, no randomness, no mutable global. Replay inputs to record: calendar dataset version, the previous-session `Candle`(s) consumed, `config_version` + threshold, and the CPR-spec version.

- **NCR20 — Provider-neutrality / architecture boundary.** CPR math lives in the **strategy/domain layer** (`app/strategies/implementations/narrow_cpr…`), **never** the Market Engine (docs/06 §228; glossary §626) and **never** the Dhan adapter. The strategy imports no provider type and reads only the broker-neutral `MarketContext`; warmup flows through the governed `HistoricalWarmupPort`. No second historical service.

- **NCR21 — Malformed / edge OHLC.** `Candle` guarantees prices `> 0`. Defensively verify `high ≥ low` (and `high ≥ close`, `low ≤ close`); on violation → `SKIPPED` (fail-closed), never a score. A **zero-width CPR** (`BC = TC`, degenerate session) is **valid** (`cpr_width = 0` = maximal narrowness), ranked narrowest — not an error.

- **NCR22 — Price-basis limitation (recorded, not solved).** Historical OHLC price basis is **undocumented** (raw vs corporate-action-adjusted; no adjustment flag sent to Dhan). V1 (single previous session) is **unaffected** (no cross-session comparison). **V2 historical-relative narrowness REQUIRES a governed price-basis / corporate-action contract as a prerequisite** — recorded as a V2 blocker, not silently solved.

- **NCR23 — Cross-instrument scanner-surface scope.** The cross-instrument scanner ranking surface (sort all F&O candidates by narrowness) **does not exist** (`rank_results`/`StrategyResultsPublished` are per-instrument-across-strategies). Building it is **out of scope** here and deferred to a separate scanner-surface governance/impl slice. This spec governs only the per-instrument narrowness **measure** and the ranking **key + tie-break**.

---

## No-look-ahead proof (NCR8)
1. The planner resolves the requirement window strictly via `previous_trading_days(anchor, …)` where `anchor`'s newest resolvable session is `previous_trading_day(reference)` — the current session is never included.
2. `supports_current_day=False` is hard-coded in `build_historical_warmup_service`; current-day intervals are withheld.
3. `evaluate(context, config, metadata)` is a pure function that reads only `context.historical.previous_session`/`.series`; it does not touch any live/current-day field. No mutable global, clock, or I/O.
⇒ Today's CPR is fully determined before `T` opens and cannot repaint.

## Failure-mode matrix (NCR14)
| Condition | Layer | Outcome |
|-----------|-------|---------|
| No/insufficient previous session | warmup readiness | `Readiness.MISSING_HISTORICAL` → strategy not-ready → no evaluation |
| Outside `CalendarCoverage` | planner | `OutsideCalendarCoverageError` → warmup unsatisfied → not-ready |
| No authoritative calendar | composition | `AuthoritativeCalendarUnavailableError` (enabled path) / `HistoricalWarmupUnavailableError` → not-ready |
| Special OPEN missing intraday timing | planner | `MissingSessionTimingError` (session timeframe unaffected; only intraday) |
| Provider/source failure | source | `HistoricalSourceError`/`HistoricalDataQualityError` → unresolved → not-ready |
| Ready but malformed previous `Candle` (defensive) | strategy | `EvaluationStatus.SKIPPED` + reason code; never MATCHED |
| Zero-width CPR | strategy | Valid `MATCHED`, `cpr_width_pct = 0` (narrowest) |

## Future implementation contract (NARROW-CPR-IMPL-R1 — do NOT implement now)
- New package `app/strategies/implementations/narrow_cpr/` (path reserved by the import-boundary test): a **pure CPR calculator** (helpers/calculator submodule) + the `NarrowCprStrategy` (descriptor/requirements/configuration/`evaluate`). No Market-Engine, calendar_data, adapter, runtime, lifecycle, or authority-flag changes.
- The strategy declares NCR13 requirements, reads `context.historical.previous_session.candle`, computes NCR2/NCR3/NCR4/NCR5, and emits NCR10 metrics with NCR11 status/ranking-key semantics.
- No change to `supports_current_day`, `staged_observation_verified`, `tick_aggregate_verified`.

## Future test matrix (governs NARROW-CPR-IMPL-R1)
A standard CPR formula; B `TC < BC` normalization; C zero-width CPR valid; D price-invariant normalized geometry across different-priced stocks; E previous session across weekend; F across NSE holiday; G across consecutive closures; H exceptional-OPEN session participates; I multi-interval completed session → normal CPR; J outside coverage fails closed; K missing previous session fails closed; L incomplete session fails closed; M provider failure never becomes a valid score; N no current-day OHLC consumed; O (V2) historical window no look-ahead; P (V2) window off-by-one (`N+1` sessions, newest = today's basis); Q insufficient history fails closed; R deterministic replay; S disabled ⇒ zero Narrow-CPR historical demand; T zero declared requirements ⇒ zero provider calls; U no Dhan import/type in strategy/calculator; V `supports_current_day` stays False; W `staged_observation_verified` stays False; X `tick_aggregate_verified` stays False; Y existing historical/calendar/runtime regressions green; Z ranking tie-break deterministic (equal `cpr_width_pct` → ascending instrument symbol).

## Files changed
Docs-only: this specification + the ADR README index row. No `app/`/`tests/` change.

## Consequences
**Positive.** The Narrow CPR mathematical, previous-session-data, no-look-ahead, result, and ranking-key contracts are frozen, explicit, cross-stock comparable, non-repainting, provider-neutral, deterministic, and reusable across the F&O scanner — with CPR being non-directional. **Negative / accepted.** Historical-relative narrowness and the cross-instrument scanner surface are deferred (each with a recorded prerequisite/blocker); the price-basis limitation is recorded, not solved. **Neutral.** No code this phase.

## Exact next slice
**NARROW-CPR-IMPL-R1** — implement the V1 pure CPR calculator + `NarrowCprStrategy` per this spec and its test matrix; all gates green; invariants preserved. The cross-instrument scanner-surface slice and V2 historical-relative narrowness (gated on a price-basis contract) follow separately.
