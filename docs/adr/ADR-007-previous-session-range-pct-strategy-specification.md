# ADR-007 Addendum — Previous Session Range % Strategy Specification (V1)

| Field | Value |
|-------|-------|
| **Type** | Subordinate strategy specification (not a numbered ADR) |
| **Subordinate to** | ADR-007 (dynamic strategy lifecycle & requirement management) |
| **Related** | ADR-007 Narrow CPR strategy specification (template), ADR-007 partial-universe historical-readiness addendum, ADR-011 (trading-calendar authority), ADR-012 (scanner + REST + frontend), ADR-013 (production registration), ADR-006 (candle completeness) |
| **Status** | Accepted (specification) — **implementation DEFERRED** to PREVIOUS-SESSION-RANGE-PCT-IMPL-R1 |
| **Date** | 2026-08-19 |
| **Deciders** | Strategy / Platform Architecture |
| **Decision** | Govern a plug-and-play, completed-session-only, non-directional scanner strategy — **Previous Session Range %** — that ranks NSE cash-equity instruments by the previous completed authoritative session's normalized range. It reuses the frozen previous-session seam, generic scanner, generic REST endpoint, and generic frontend, and requires **no** current-session authority. |

---

## Context

Open=High/Open=Low is BLOCKED on current-session OHLC authority (both source bits False; ADR-008/009 gates FAILED; ADR-009 CSOA enablement path). This strategy is deliberately authority-independent: it consumes only `context.historical.previous_session` — the same authoritative seam Narrow CPR already uses in production — so it can ship while the current-session authority track remains blocked. Verified template: `app/strategies/implementations/narrow_cpr/strategy.py` (declares `HistoricalRequirement(Timeframe.session(), lookback=1)` + `FactNeed.PREVIOUS_SESSION`, trigger `ON_HISTORICAL_READY`, emission `ONE_SHOT_PER_SESSION`, category `MARKET_STRUCTURE`, `candle_completeness=AUTHORITATIVE_ONLY`, `score=None`, named `MetricEntry` metrics). `PreviousSessionFacts.candle` is the authoritative `Candle` exposing `open_price`/`high_price`/`low_price`/`close_price`. The scanner is generic (`ScannerRankingPolicy(strategy_id, metric_name, ordering ∈ {ASCENDING, DESCENDING})`, instrument-ascending tie-break).

## Decisions PSR1–PSR24

- **PSR1 — Canonical input.** Only the previous completed authoritative trading session, read as `context.historical.previous_session` (`PreviousSessionFacts.candle`). No current-session input of any kind.
- **PSR2 — Formula.** `previous_range = previous_high − previous_low`; **`previous_range_pct = previous_range / previous_open × 100`** (formula A).
- **PSR3 — Normalization rationale.** Dividing by the session's own `open` makes the metric price-scale invariant and cross-stock comparable, is the conventional "range as % of open", and uses a field (`open`) that Narrow CPR's pivot metric ignores — reinforcing independence. Rejected: `/close` (equivalent but less conventional as a range base), `/mid` and `/pivot` (pivot overlaps Narrow CPR's CPR geometry), raw `high−low` (not scale-invariant → not comparable).
- **PSR4 — Decimal semantics.** All arithmetic in `Decimal`; no float; no internal rounding/quantization. Domain: `previous_open > 0`, `previous_high ≥ previous_low`, `previous_range ≥ 0`, `previous_range_pct ≥ 0`. Display rounding is a frontend concern.
- **PSR5 — V1 product purpose.** **Range EXPANSION** — identify instruments whose previous completed session showed the largest normalized range (activity/volatility). Chosen over compression because Narrow CPR already ranks compression; expansion is a genuinely independent feature.
- **PSR6 — Scanner ordering.** `ScannerRankingPolicy(strategy_id="previous_session_range_pct", metric_name="previous_range_pct", ordering=DESCENDING)` — largest range = rank 1. Canonical `(exchange, symbol)` ascending tie-break unchanged. No scanner modification.
- **PSR7 — Score policy.** `score=None`. Ranking uses the real `previous_range_pct` metric; no arbitrary normalized score is invented.
- **PSR8 — Match / no-match policy.** **Rank-all**: every instrument with a valid authoritative previous session returns `MATCHED` (reason `PREVIOUS_SESSION_RANGE_VALID`). No threshold in V1.
- **PSR9 — Configuration.** Minimal — a `PreviousSessionRangePctConfiguration` carrying only `config_version` (no strategy-specific parameter in V1). Explicitly excluded: multi-session window, percentile, z-score, weights, direction, momentum, ATR, volume, VWAP, current-session filters. (An optional `min_range_pct` inclusive threshold is a possible future addition, not V1.)
- **PSR10 — Historical requirement.** Exactly `HistoricalRequirement(timeframe=Timeframe.session(), lookback=1)`. No `lookback > 1`, no current day, no extra request for ranking.
- **PSR11 — Fact requirement.** `FactNeed.PREVIOUS_SESSION`, same as Narrow CPR. **Must not** declare `FactNeed.SESSION_STATISTICS`.
- **PSR12 — Trigger.** `StrategyTrigger.ON_HISTORICAL_READY` — the value is fully determined once authoritative previous-session history is installed. No tick/clock trigger.
- **PSR13 — Emission.** `EmissionPolicy.ONE_SHOT_PER_SESSION` — the input is immutable for today's session, so exactly one emission per instrument per trading day (no provisional/retraction problem).
- **PSR14 — No-look-ahead.** `NO_LOOK_AHEAD = YES`. Reads only completed previous-session facts; no current-day field is consulted.
- **PSR15 — Partial-universe semantics.** Reuse ADR-007 partial model: infrastructure warmup success → strategy RUNNING; an instrument lacking previous-session history → `MISSING_HISTORICAL` at readiness → skipped → no `StrategyEvaluation`, no candidate. Scanner: `expected_count` = universe size, `evaluated_count` = material evaluations, `eligible_count` = ranked matches, `completeness = PARTIAL` when not all evaluate. Never fabricate `previous_range_pct=0` for a missing instrument. (Absent-previous-session on a stray evaluation → `SKIPPED`, reason `PREVIOUS_SESSION_RANGE_NO_PREVIOUS`, fail-closed.)
- **PSR16 — Calendar semantics.** "Previous session" = previous completed authoritative session via ADR-011 (Market-Engine-resolved). The strategy contains **no** weekend/holiday/special-session/date arithmetic and never does `reference_date − 1 day`.
- **PSR17 — Multi-interval transparency.** The strategy receives one canonical whole-session `Candle` and is blind to whether the session was ordinary/shortened/single-/multi-block/special-open (ADR-011 multi-interval addendum handles aggregation). No `TradingInterval` logic in the strategy.
- **PSR18 — Price-basis limitation.** The metric is a **same-session** ratio. A corporate-action adjustment applies one uniform multiplicative factor to all of a session's prices, which cancels in `(high−low)/open` — so the V1 metric is invariant to adjusted-vs-unadjusted basis (one `Candle` = one basis; no intra-session mixing exists). V1 proceeds. Multi-session relative comparisons (not done here) would require stronger price-basis governance.
- **PSR19 — Output metrics.** Named `MetricEntry` tuple: `previous_range_pct`, `previous_range`, `previous_open`, `previous_high`, `previous_low`, `previous_close`, `source_session_date` (ISO). All from the previous authoritative session; no current-day metric; no fake score.
- **PSR20 — Reason codes.** Structured constants (no English): `PREVIOUS_SESSION_RANGE_VALID` (rank-all match), `PREVIOUS_SESSION_RANGE_NO_PREVIOUS` (absent previous session → SKIPPED). No directional reason codes. Missing history stays at readiness level, not a fabricated result.
- **PSR21 — Provider neutrality.** No imports/references to Dhan, httpx, `security_id`, `exchange_segment`, WebSocket, Redis, or SQLAlchemy. Reads only the broker-neutral `MarketContext`/domain contracts. No `if provider == "dhan"`.
- **PSR22 — Registration.** One new strategy package (`app/strategies/implementations/previous_session_range_pct/`: pure calculator + configuration + strategy) plus one `production_catalog` `StrategyCatalogEntry` (strategy + default config + `ScannerRankingPolicy`). Explicit `strategies_enabled` behavior unchanged; no auto-discovery; no Dhan-specific registration.
- **PSR23 — Scanner / REST / frontend reuse.** The generic `CrossInstrumentStrategyScanner` ranks `previous_range_pct` DESCENDING with **no** modification and **no** `strategy_id` branch; the generic `GET /api/v1/scanners/{strategy_id}` serves `/api/v1/scanners/previous_session_range_pct`; the frontend reuses `getScannerSnapshot`/`useScannerSnapshot`/`ScannerTable`/`ScannerPanel`/PARTIAL-COMPLETE UX/refresh/15s polling/limit — the future frontend delta is only presentation metadata + route + nav entry.
- **PSR24 — Current-session authority isolation.** Requires none of `SessionStatistics`, `FactNeed.SESSION_STATISTICS`, `staged_observation_verified`, `tick_aggregate_verified`, `supports_current_day`. Runnable while Open=High/Low stays blocked. Production authority bits remain False; `supports_current_day=False`.

## Task / concurrency

`NEW create_task SITES = 0`. Reuses historical warmup, StrategyManager, EventBus, and the scanner. `market_runtime.py` create_task count stays **3**.

## Consequences

- A second, independent, non-directional scanner ships on the frozen completed-session infrastructure with zero authority change and zero new tasks.
- Narrow CPR V1 is untouched; the two strategies are independent plug-ins sharing only generic infrastructure.
- Implementation deferred to PREVIOUS-SESSION-RANGE-PCT-IMPL-R1 (backend), then a thin frontend slice (metadata + route + nav).
