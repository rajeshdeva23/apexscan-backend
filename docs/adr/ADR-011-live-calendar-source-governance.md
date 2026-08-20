# ADR-011 Addendum — Live/Historical Calendar-Source Governance

| Field | Value |
|-------|-------|
| **Type** | Subordinate governance addendum (not a numbered ADR) |
| **Subordinate to** | ADR-011 — Historical Trading-Calendar Authority Window |
| **Related** | ADR-010 (runtime lifecycle & live composition), ADR-004, ADR-011 multi-interval + calendar-monitor addenda, ADR-011 NSE calendar evidence record |
| **Status** | Accepted (decision + implementation contract); **implementation DEFERRED** to ADR-011-LIVE-CALENDAR-IMPL |
| **Date** | 2026-08-16 |
| **Deciders** | Market-Engine / Platform Architecture |
| **Outcome** | **B — date-level calendar should be unified** onto the packaged dataset for the live classifier (within coverage), keeping intraday session-hours separate. |

---

## Context

ADR-011-IMPL routed **completed-session historical** planning onto the authoritative packaged
`TradingCalendarDataset` (16 closed + 2 open + 1 override for 2026). The **live**
`MarketSessionClassifier` remained on `settings.nse_holidays`. Both `MarketSessionClassifier.from_settings`
(`session.py:336`) and `_schedule_and_calendar` (`market_runtime.py:185`) build
`TradingCalendar(holidays=holidays)` — **no `open_sessions`, no `closed_dates`** beyond the
(default-empty) `nse_holidays` CSV. This phase decides whether the dual arrangement is safe.

## Calendar consumer map

| Consumer | Current source | Uses calendar for |
|----------|----------------|-------------------|
| `HistoricalCalendarWindow`, `HistoricalRangePlanner`, `historical/resampling`, `historical/session_candles` | **dataset** (post ADR-011-IMPL) | completed-session trading-day resolution / reconstruction |
| `MarketSessionClassifier` (built in `LiveMarketRuntime`) | **settings.nse_holidays** | live trading-date + session phase |
| `TickEngine.process` → `classifier.classify` | via live classifier | per-tick session classification |
| `SessionStatisticsRefreshDriver` → `classifier.classify` | via live classifier | LIVE_SESSION phase gating |
| Live `CandleEngine` | `SessionSchedule` (hours only, **not** the calendar) | intraday bucket bounds |
| Future Dhan calendar monitor | should target the **dataset** | secondary discrepancy detection |

## Same-date contradiction analysis (date-level)

| Date class | Live (settings) | Historical/reality (dataset) | Contradiction? |
|------------|-----------------|------------------------------|----------------|
| Ordinary weekday (2026-07-01) | trading | trading | no |
| Weekday holiday (2026-01-26 Republic Day) | **trading** (settings empty) | closed | **YES** — live mis-open on a holiday (data-flow-inert: no ticks on a holiday, but session-state wrong) |
| Amended holiday (2026-01-15) | **trading** | closed | **YES** — same class |
| Weekend (ordinary Sat/Sun) | closed | closed | no |
| **Exceptional Sunday OPEN (2026-02-01 Budget, 2026-11-08 Muhurat)** | **closed** (weekend; classifier has no `open_sessions`) | **trading** | **YES — real correctness gap:** live ticks WILL arrive that day and would be mis-classified/withheld |
| Outside coverage (2025 / 2027) | settings rule (empty ⇒ all weekdays trading) | `OutsideCalendarCoverageError` (fail-closed) | divergent handling |

**Verdict:** the dual arrangement is **not simply safe**. The holiday divergence is largely
data-flow-inert (no ticks on a real closure), but the **exceptional weekend-OPEN divergence is a
genuine live correctness gap** — on 2026-02-01 / 2026-11-08 the market is open and the live
classifier (no `open_sessions`) would treat it as closed.

## Distinctions preserved
- **Date-level authority** ("is this a trading day?", incl. `open_sessions`) — this is what should
  be unified onto the dataset.
- **Intraday session-hours authority** (exceptional live session timing / multi-interval) — remains
  **out of scope** (multi-interval addendum "Live candle engine scope"). The live `CandleEngine`
  keeps the default `SessionSchedule`; the historical `EffectiveSchedule`/overrides are **not** wired
  into live. Current-day exceptional live scheduling stays a separate future concern.

## Decision — OUTCOME B

The live `MarketSessionClassifier` shall consume the **dataset-derived `TradingCalendar`**
(`closed_dates` + `open_sessions`) for **date-level** trading-day classification, **within the
dataset's `CalendarCoverage`**. Constraints:

1. **Date-level only.** Unify trading-day identity; do **not** wire the historical
   `EffectiveSchedule`/multi-interval overrides into the live `CandleEngine` (intraday hours stay
   settings/`SessionSchedule`-governed; exceptional live hours out of scope).
2. **Coverage-aware, fail-closed (§8).** The dataset is authoritative only inside its coverage
   (currently 2026). When the current/live date is **outside** coverage, the runtime must adopt an
   **explicit unprovisioned/fail-closed** policy (startup error or an explicit degraded live-calendar
   state) — **never** a silent "all weekdays trade" fallback and never a silent settings fallback.
   The exact out-of-coverage policy is decided in the IMPL slice.
3. **`settings.nse_holidays` role (§5): legacy / non-authoritative.** It must **not** remain an
   independent authoritative copy alongside the dataset. Within coverage, the dataset is the sole
   date-level authority. `nse_holidays` is retained (if at all) only for the disabled/no-dataset or
   out-of-coverage path per the IMPL migration, or removed — never duplicating the 16 dates.
4. **Invariants unchanged.** `supports_current_day=False`; `staged_observation_verified=False`;
   `tick_aggregate_verified=False`; no new task; provider-neutral Market Engine; no calendar network
   dependency; the Dhan page never becomes authority.

This is **not** a deep architectural conflict (Outcome C): unifying date-level classification uses
existing types (`TradingCalendar` already supports `open_sessions`) and the ADR-010 composition seam;
it does not require reopening an Accepted decision or enabling current-day/authority.

## Decision matrix

| Consumer | Current source | Required semantics | Dataset can serve? | Should unify? | Risk if not unified |
|----------|----------------|--------------------|--------------------|---------------|---------------------|
| Historical planner/reconstruction | dataset | completed-session date auth (2026) | yes (done) | already unified | — |
| `MarketSessionClassifier` | settings.nse_holidays | live date-level incl. open_sessions, within coverage | **yes (date-level)** | **YES** | mis-open on holidays; **fails exceptional-OPEN days (real data mishandled)** |
| Live runtime session gating (refresh driver) | via classifier | LIVE_SESSION incl. exceptional opens | yes | YES | refresh mis-gates on exceptional-open days (moot while authority off, but wrong) |
| RequirementsCoordinator | via historical warmup | — | already dataset | — | — |
| Future calendar monitor | dataset (target) | one authoritative date-level calendar | yes | targets dataset already | — |
| Strategy readiness | historical (dataset) + live gating | mixed | historical yes | historical unaffected | live-gated strategies affected on exceptional days only |
| Live `CandleEngine` | `SessionSchedule` (hours) | intraday bucket hours | **not wired** | **NO (keep separate)** | exceptional live intraday hours out of scope |

## Strategy impact (§12)
- **Narrow CPR / PREVIOUS_SESSION facts** — historical, dataset-backed → **unaffected**; can proceed independently of this decision.
- **Open=High / Open=Low** — blocked on session-statistics authority (P4.6E6C) regardless; also live-session-dependent, so benefits from B later, but not unblocked by it.
- **Runtime session gating / readiness** — improved by B on exceptional-open days; no concrete strategy exists, so nothing is blocked today.

## Monitor interaction (§11)
The future `ADR-011-CALENDAR-MONITOR-IMPL` compares the Dhan secondary observation against the **dataset** (the one authoritative date-level calendar) — true under both the status quo and Outcome B. The monitor remains **secondary/discovery only** and never mutates authority. Monitor implementation may proceed against the dataset regardless of when B lands.

## Files changed
Docs-only: this artifact + the README index row. **No production/test code changed** in this governance phase.

## Exact next slice
**ADR-011-LIVE-CALENDAR-IMPL** — build `LiveMarketRuntime`'s `MarketSessionClassifier` from the
dataset-derived `TradingCalendar` (date-level, incl. `open_sessions`) when provisioned, keeping
`SessionSchedule` hours from settings and **not** wiring intraday overrides into the live
`CandleEngine`; define + implement the out-of-coverage fail-closed policy; demote/remove
`settings.nse_holidays` as authority per migration. Preserve all invariants; full gates.
(Independent of, and may be sequenced with, `ADR-011-CALENDAR-MONITOR-IMPL`.)

## Recommendation
Adopt Outcome B and schedule ADR-011-LIVE-CALENDAR-IMPL. The calendar monitor and any
historical-only strategy work (e.g. Narrow CPR) may proceed in parallel — they already reference the
dataset. Keep intraday exceptional-hours out of scope until separately governed.
