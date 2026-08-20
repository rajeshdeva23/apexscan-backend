# ADR-007 Addendum — Previous Session Body % Strategy Specification (V1)

| Field | Value |
|-------|-------|
| **Type** | Subordinate strategy specification (not a numbered ADR) |
| **Subordinate to** | ADR-007 (dynamic strategy lifecycle & requirement management) |
| **Related** | ADR-007 Narrow CPR + Previous Session Range % strategy specifications (templates), ADR-007 partial-universe addendum, ADR-011 (calendar authority), ADR-012 (scanner + REST + frontend), ADR-013 (registration), STRATEGY-V1-FREEZE-AND-NEXT-SELECTION-R1 |
| **Status** | Accepted (specification) — **implementation DEFERRED** to PREVIOUS-SESSION-BODY-PCT-IMPL-R1 |
| **Date** | 2026-08-20 |
| **Deciders** | Strategy / Platform Architecture |
| **Decision** | Govern a plug-and-play, completed-session-only, **non-directional** scanner — **Previous Session Body %** — ranking NSE cash-equity instruments by the previous completed session's absolute candle-body size as a percentage of its open. Reuses the frozen previous-session seam, generic scanner, generic REST, and generic frontend; needs no current-session authority. |

---

## Context

Selected as the third scanner (STRATEGY-V1-FREEZE-AND-NEXT-SELECTION-R1, OUTCOME A) because it runs entirely on already-authoritative previous-session data and adds a structural dimension independent of Narrow CPR (pivot compression) and Previous Session Range % (`(high−low)/open`): body% measures the open→close conviction, direction-agnostic. It reuses the exact contract shape proven by the two frozen V1 strategies (`PreviousSessionFacts.candle`, generic `ScannerRankingPolicy`, generic REST/frontend). `Candle` guarantees `open>0` and `low ≤ open,close ≤ high`.

## Decisions PSB1–PSB27

- **PSB1 — Canonical input.** Only the previous completed authoritative session via `context.historical.previous_session` (`PreviousSessionFacts.candle`), fields `open_price` and `close_price`. No current-session input; no volume/LTQ/OI/VWAP/order-book/indicator/provider field.
- **PSB2 — Formula.** `previous_body = |previous_close − previous_open|`; **`previous_body_pct = previous_body / previous_open × 100`**.
- **PSB3 — Decimal semantics.** `Decimal` only, under a fixed `localcontext(prec=28)` (same convention as the CPR/PSR calculators); no float; no rounding/quantize before ranking.
- **PSB4 — Direction neutrality.** The absolute value makes it direction-agnostic: `open=100,close=110` and `open=100,close=90` both give `body=10`, `body_pct=10`. No BUY/SELL/LONG/SHORT/BULLISH/BEARISH/CE/PE/entry/exit/target/stop output; no directional score or metadata.
- **PSB5 — Zero-body (doji).** `open == close` ⇒ `body=0`, `body_pct=0`. Valid and evaluated (rank-all); never skipped, never a fabricated minimum, never an error.
- **PSB6 — Input validity.** The pure calculator fails closed (`PreviousSessionBodyInputError`) on `open ≤ 0`. Canonical `Candle` already guarantees `open>0` (and body ≥ 0 by construction), so this is unreachable in the strategy path — no duplicated validation in the strategy.
- **PSB7 — Price-scale invariance.** Dimensionless: `open=100,close=110` and `open=1000,close=1100` both give `body_pct=10`, so the metric is cross-instrument comparable.
- **PSB8 — Corporate-action / price basis.** A uniform corporate-action multiplier `k` applied to a session's prices cancels in `|k·close − k·open| / (k·open) = |close−open|/open`; one `Candle` = one basis, so the V1 metric is **safe**. This does **not** generalize to multi-session comparisons (which remain ungoverned).
- **PSB9 — Configuration.** `PreviousSessionBodyPctConfiguration` carrying only `config_version`; no threshold/percentile/z-score/lookback/direction/weight field. V1 ranks the available universe (rank-all).
- **PSB10 — Historical requirement.** Exactly `HistoricalRequirement(Timeframe.session(), lookback=1)`. No lookback>1; no current day.
- **PSB11 — Fact requirement.** `FactNeed.PREVIOUS_SESSION`; **must not** declare `FactNeed.SESSION_STATISTICS`.
- **PSB12 — Trigger.** `StrategyTrigger.ON_HISTORICAL_READY`. No tick/clock/live-session trigger.
- **PSB13 — Emission.** `EmissionPolicy.ONE_SHOT_PER_SESSION` — the previous completed session is immutable during today's session.
- **PSB14 — Result metrics.** Named `MetricEntry` tuple: `previous_body_pct`, `previous_body`, `previous_open`, `previous_close`, `source_session_date`. High/low are **excluded** (not part of the V1 calculation; no debugging convention requires them).
- **PSB15 — Score policy.** `score = None` (no fabricated score).
- **PSB16 — Ranking.** `ScannerRankingPolicy(strategy_id="previous_session_body_pct", metric_name="previous_body_pct", ordering=DESCENDING)`. Rank 1 = largest previous-session body % (a ranking metric only — not "best/strongest/bullish/bearish/high-probability").
- **PSB17 — Tie-break.** Reuse the canonical generic `(exchange, symbol)` ascending tie-break; no body-specific tie-break.
- **PSB18 — Missing history.** Absent `previous_session` ⇒ `SKIPPED`, reason `PREVIOUS_SESSION_BODY_NO_PREVIOUS` (fail-closed; readiness normally gates this). Never fabricate `open/close/body/body_pct = 0`; missing history ≠ a valid zero-body session.
- **PSB19 — Partial-universe.** Reuse ADR-007: RUNNING on infra success; per-instrument missing history ⇒ `MISSING_HISTORICAL` ⇒ skipped ⇒ no candidate; counts `expected/evaluated/eligible`, `completeness=PARTIAL`; never fabricated; never whole-strategy ERROR.
- **PSB20 — No-look-ahead / non-repainting.** Reads only completed previous-session facts; changing today's tick/price/SessionStatistics with identical `PreviousSessionFacts` cannot change the result ⇒ `NO_LOOK_AHEAD = YES`, non-repainting for today.
- **PSB21 — Scanner reuse.** Generic `CrossInstrumentStrategyScanner` unchanged; no `if strategy_id` branch; no ranking inside the strategy.
- **PSB22 — REST reuse.** Generic `GET /api/v1/scanners/{strategy_id}` serves `/scanners/previous_session_body_pct`; Decimal-as-string transport; no body-specific schema.
- **PSB23 — Frontend reuse (deferred).** Future route `/scanners/previous-session-body-pct`, title "Previous Session Body %", metric label "Previous Body %", `formatMetric: formatPercent` (already generic). Renders backend order/rank; no client calc/re-rank/direction. Generic client/hook/`ScannerPanel`/`ScannerStatusBar`/`ScannerTable` unchanged. Not implemented in this phase.
- **PSB24 — Provider neutrality.** Reads only the broker-neutral `MarketContext`/canonical `Candle`; no Dhan/httpx/security_id/exchange_segment/WebSocket/Redis/SQLAlchemy.
- **PSB25 — Current-session authority isolation.** Requires none of `SessionStatistics`/`SESSION_STATISTICS`/`staged_observation_verified`/`tick_aggregate_verified`/`supports_current_day`; runs with all bits False; Open=High/Low unrelated/blocked.
- **PSB26 — Task impact.** `NEW create_task SITES = 0`; `market_runtime.py` count stays 3.
- **PSB27 — Frozen strategies.** Narrow CPR V1 and Previous Session Range % V1 unchanged (calculators/config/strategies/policies/results/routes/presentation/tests). Any required change to either is a STOP.

## Presentation wording (deferred FE)

Subtitle: "Stocks ranked by the absolute candle-body size of the previous completed session as a percentage of its open. A larger previous body % ranks higher." No profitability/strength/momentum/buy/sell language.

## Future test matrix

Calculator: positive body, negative-direction body (equal abs), equal absolute bodies, zero body (doji), fractional/non-terminating Decimal, price-scale invariance, invalid standalone `open≤0` fail-closed. Strategy: descriptor/requirements (session,1 / PREVIOUS_SESSION / not SESSION_STATISTICS / ON_HISTORICAL_READY / ONE_SHOT_PER_SESSION / MARKET_STRUCTURE), MATCHED rank-all + score None + metrics, SKIPPED on missing previous, no-look-ahead, determinism, no directional output, wrong-config TypeError. E2E: enabled → RUNNING → snapshot; 208 partial (205/3) + complete + one-missing + zero-ready; DESCENDING rank-1 = largest body %; canonical tie-break; REST 200 + Decimal string + `?limit=20` counts intact. Registration: catalog entry + DESCENDING policy; disabled → zero historical demand; enabled → single session lookback.

## Platform impact

Zero production change to: Market Engine, `HistoricalWarmupService`, planner, calendar, StrategyManager, readiness, partial-universe, `CrossInstrumentStrategyScanner`, scanner REST endpoint/schema, generic frontend client/hook/`ScannerPanel`/`ScannerStatusBar`/`ScannerTable`. PostgreSQL / Redis / WebSocket / persistence — NOT REQUIRED. Authority bits unchanged.

## Expected implementation files (deferred)

`app/strategies/implementations/previous_session_body_pct/{__init__,calculator,configuration,strategy}.py` + one `production_catalog` entry with the DESCENDING `ScannerRankingPolicy` + focused calculator/strategy/registration/E2E tests. Frontend follows in a separate slice.

## Consequences

- A third completed-session scanner ships on the frozen platform with zero generic/backend-infra change and zero new tasks.
- The two V1 strategies stay frozen; all three are independent plug-ins sharing only generic infrastructure.
- Implementation deferred to PREVIOUS-SESSION-BODY-PCT-IMPL-R1.
