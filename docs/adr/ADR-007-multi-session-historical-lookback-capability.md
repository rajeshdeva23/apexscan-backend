# ADR-007 Addendum — Multi-Session Historical Lookback Capability (session lookback > 1)

| Field | Value |
|-------|-------|
| **Type** | Subordinate capability-governance artifact (not a numbered ADR) |
| **Subordinate to** | ADR-007 (dynamic strategy lifecycle & requirement management) |
| **Related** | ADR-006 (candle completeness), ADR-011 (calendar authority + multi-interval/coverage addenda), ADR-007 partial-universe historical-readiness addendum, ADR-012 (scanner) |
| **Status** | Accepted (capability governance) — **no code change; validation DEFERRED to MULTI-SESSION-HISTORICAL-LOOKBACK-VALIDATION-R1** |
| **Date** | 2026-08-20 |
| **Deciders** | Market-Engine / Platform Architecture |
| **Decision** | The existing historical subsystem **already supports** `HistoricalRequirement(Timeframe.session(), lookback=N)` with N>1, deterministically populating `HistoricalContext.series` with N completed authoritative session candles (oldest→newest, current-day excluded, calendar-aware). The **technical lookback capability is READY for a dedicated E2E validation**. Separately, **cross-session raw-price operations remain BLOCKED** on ungoverned historical price basis; per-session dimensionless-normalized operations are basis-safe. |

---

## Evidence (read-only inspection; `backend/`)

- **Series population (the load-bearing question):** `HistoricalWarmupService._assemble` builds `HistoricalContext(previous_session=…, series=(…,))` from the warmed candles (`service.py:533-544`); `_direct_series` trims to the full lookback — `candles=prepared[-requirement.lookback:]` (`service.py:567`) and returns `None` when `len(prepared) < lookback` (`service.py:564-565`). The session path installs **both** a full N-candle `HistoricalSeries` in `series` **and** a derived single-session `previous_session` (`service.py:540`, `_previous_session` → `candles[-1]`). N-candle `series` for lookback>1 is confirmed.
- **Ordering:** `HistoricalSeries` normalizes candles to ascending `(start,end)` and rejects empty/duplicate/overlapping intervals + mixed instruments (`context.py:53-79`); `HistoricalContext` allows at most one series per timeframe (`context.py:150-158`). Oldest→newest is guaranteed at construction.
- **Union:** `HistoricalRequirementRegistry.effective_requirements` folds **max lookback per timeframe** (not sum) (`requirements.py:107-117`).
- **Calendar-aware window:** `previous_trading_days` → `previous_trading_day` walks the authoritative calendar day-by-day, skipping non-trading days, fail-closed at the coverage bound (`calendar_window.py:79-123`); never `date − N`.
- **Fetch shape:** session timeframe → direct daily bars (`source.py:28,43-58`), then canonicalized to `[regular_open, regular_close)` session identity by `canonical_session_series` (`session_candles.py:74-99`) — not resampled from intraday.
- **Current-day exclusion:** the window is strictly < reference (`calendar_window.py:92`); `supports_current_day=False` (`service.py:359`, `strategy_requirements_wiring.py:151`) additionally withholds current-day reconciliation (`service.py:673-680`). Both exclude today; they are distinct gates.
- **Cache/concurrency/tasks:** reuses `HistoricalCoordinator` (Semaphore bound, default 8; `coordinator.py:47,103`) + `_inflight` coalescing (`coordinator.py:84-89`) + `HistoricalCache` containment (`cache.py:30-32`). No new `asyncio.create_task` site; only pre-existing `ensure_future`/`gather` (`coordinator.py:86`, `service.py:499`). `market_runtime.py` create_task count stays 3.

## Decisions MSH1–MSH16

- **MSH1 — Lookback semantics.** `HistoricalRequirement(Timeframe.session(), N)` = the **N most recent completed authoritative trading sessions strictly before the reference date D**, ordered oldest→newest (`[D-N … D-1]`). Trading sessions, not calendar days.
- **MSH2 — Series population.** `HistoricalContext.series` carries one session `HistoricalSeries` of exactly N candles for N>1; `previous_session` is the newest (`D-1`) derived from the same series. Both are populated; neither replaces the other.
- **MSH3 — Ordering.** Oldest→newest, deterministic, guaranteed at construction. Strategies must consume `series[-1]=D-1`, `series[-2]=D-2`, … and must **not** locally sort provider data.
- **MSH4 — Requirement union.** Max lookback per timeframe across all enabled consumers (`max(1,5,20)=20`); one shared warmup; no per-strategy warmup subsystem.
- **MSH5 — Calendar traversal.** Authoritative ADR-011 calendar/coverage: weekends and holidays skipped; exceptional OPEN sessions included; a multi-interval completed session is one canonical whole-session candle; outside coverage fails closed. No date arithmetic in the strategy.
- **MSH6 — Current-day exclusion.** Newest allowed session is `D-1` (structural planner window), reinforced by `supports_current_day=False`. No current-day candle, live substitution, or SessionStatistics substitution.
- **MSH7 — Completeness.** `series` holds only authoritative **complete** session candles — never partial/incomplete/synthetic/current-day.
- **MSH8 — Insufficient history (two distinct outcomes).** (a) Calendar cannot supply N sessions from D (near coverage start) → `OutsideCalendarCoverageError` propagates → **GLOBAL** START ERROR (the requirement can't be satisfied for anyone). (b) Provider returns M<N candles for a covered window → `_direct_series` returns `None` → that instrument's timeframe unresolved → **LOCAL** `MISSING_HISTORICAL`, instrument skipped, scanner PARTIAL. Never silently M; never fabricated.
- **MSH9 — Partial-universe (unchanged for N>1).** Infra warmup success → RUNNING; per-instrument local gaps → skipped → PARTIAL; `expected_count` = universe size; no global ERROR for local gaps. Global calendar/authority failures stay fail-closed (MSH8a).
- **MSH10 — Error taxonomy.** GLOBAL (→ START ERROR): `OutsideCalendarCoverageError`, `HistoricalWarmupUnavailableError`, `AuthoritativeCalendarUnavailableError` (composition-time, fail-fast). LOCAL per-plan (→ instrument not-ready/PARTIAL): `HistoricalSourceError`, `HistoricalDataQualityError`, `MissingSessionTimingError`. The known MissingSessionTiming governance-vs-code discrepancy is **moot for `Timeframe.session()`**: a pure session requirement returns before `_guard_special` (`service.py:217`) and never triggers it.
- **MSH11 — Fetch / cache / coalescing / tasks.** Full reuse of the existing coordinator/cache with bounded concurrency + request coalescing; a larger window serves smaller lookbacks. **Zero** new task sites; `create_task` count stays 3.
- **MSH12 — Provider neutrality.** `HistoricalRequirement`/`HistoricalContext`/`HistoricalSeries` expose only canonical `Candle`/`Instrument`/`Timeframe`; no Dhan `security_id`/`exchange_segment`/response types. A future multi-session strategy consumes canonical candles only.
- **MSH13 — Candle identity / trading-date.** Deterministic **ordered** consumption by series index is safe across ordinary/weekend-skipped/holiday-skipped/shortened/exceptional-open/multi-interval sessions (each candle's `start_timestamp` is that session's canonical regular_open). The canonical `Candle` carries **no exchange-local `trading_date`**; only the newest session has an authoritative `trading_date` (via `PreviousSessionFacts`). Per-candle authoritative trading-date **labels for older sessions are not exposed** — and are **not required** for index-ordered normalized-metric strategies (NR-k, relative-range, percentile). If a future strategy genuinely needs authoritative per-session dates for older sessions, that is a **small additive contract change** (a dated multi-session facts structure supplied by the Market Engine), not a strategy-side tz/date computation.
- **MSH14 — Price-basis boundary (inspected, NOT solved).** `PRICE_BASIS = NOT_PROVEN`: neither provider documentation nor repository governance states whether Dhan historical candles are adjusted/unadjusted/split-adjusted (grep of docs + adapter found no corporate-action semantics). **Capability A** (retrieve/order N completed authoritative session candles) is READY. **Capability B** (mathematically compare raw price *levels* across sessions) is BLOCKED pending price-basis governance. They must not be conflated.
- **MSH15 — Safe / unsafe multi-session operation matrix.** Operations on **per-session dimensionless normalized percentages** are basis-safe (each % is independently invariant to a uniform per-session multiplicative factor); operations on **raw price levels / cross-session raw-price ratios** are basis-sensitive:
  - A. NR-k on per-session normalized range % — **SAFE NOW**
  - B. Compare raw high−low ranges across sessions — **REQUIRES PRICE-BASIS GOVERNANCE**
  - C. Compare per-session body_pct across sessions — **SAFE NOW**
  - D. Percentile/rank of individually-normalized session percentages — **SAFE NOW**
  - E. Compare raw close prices across sessions — **REQUIRES PRICE-BASIS GOVERNANCE**
  - F. Multi-session return (close_D-1 / close_D-k) — **REQUIRES PRICE-BASIS GOVERNANCE** (unadjusted history gives spurious returns across corporate actions)
  - G. Rolling average of normalized range_pct — **SAFE NOW**
  - H. Historical CPR-width percentile (per-session cpr_width_pct) — **SAFE NOW**
  - (All "SAFE NOW" items are gated only on the E2E validation of the technical capability, not on price basis.)
- **MSH16 — Validation plan.** Before shipping any lookback>1 strategy, run MULTI-SESSION-HISTORICAL-LOOKBACK-VALIDATION-R1 using a **temporary test-only** requirement consumer (not a production strategy), proving the §19 matrix (A–Z) at lookback 2/5/20 on deterministic offline data.

## Consequences

- A multi-session strategy formulated on per-session normalized percentages (NR-k, relative-range, CPR-width percentile) is **architecture-ready** pending E2E validation; no framework redesign.
- Raw cross-session price/return strategies remain blocked on a future price-basis governance slice.
- Frozen strategies, scanner, REST, authority bits, and task topology are untouched.

## Outcome

**OUTCOME A** (existing architecture safely supports lookback>1 → proceed to E2E validation) **coexisting with a price-basis blocker** for raw cross-session price operations (per §25).
