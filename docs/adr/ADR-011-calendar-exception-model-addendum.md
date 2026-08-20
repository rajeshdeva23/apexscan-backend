# ADR-011 Addendum — Historical Trading-Calendar Exception Model

| Field | Value |
|-------|-------|
| **Type** | Subordinate addendum (not a numbered ADR) |
| **Subordinate to** | ADR-011 — Historical Trading-Calendar Authority Window |
| **Status** | Accepted |
| **Date** | 2026-08-14 |
| **Deciders** | Market-Engine Architecture |
| **Changes to ADR-011** | None — ADR-011's Accepted decisions are unchanged; this addendum extends the calendar *model* ADR-011 presumed authoritative |

---

## Why an addendum (artifact choice)

ADR-011 is Accepted and immutable. It governed `CalendarCoverage` as the *authority
window* over the **existing** `TradingCalendar`. It did not decide whether that
calendar can represent every real NSE session exception. ADR-011-DATA then found the
binary model insufficient (it cannot represent a weekend-open session). Resolving that
is an **extension** of the calendar model, not a change to any ADR-011 decision — so it
is governed as a **subordinate addendum**, exactly as the ADR-009 refresh-phase addendum
operationalizes ADR-009 without renumbering. If repository owners prefer a numbered
ADR-012 for a model extension, this content transfers verbatim; the default here follows
the established subordinate-artifact precedent.

## Context (confirmed by code inspection)

Current `TradingCalendar.is_trading_day` (`app/market_engine/session.py:95-99`):

```
if trading_date.weekday() in weekend_days:  # {Sat, Sun}
    return False
return trading_date not in holidays
```

i.e. `weekend ⇒ CLOSED`; `holiday ⇒ CLOSED`; otherwise `OPEN`. There is **only** a
closed-override set (`holidays`) and an unconditional weekend closure. **No open-override
seam exists.** NSE conducts weekend-open cash-equity sessions (Muhurat; special
Saturday BCP/DR sessions), so this model cannot authoritatively classify them.

Session-hours are also **global, not per-date**: `bucket_bounds` / `session_buckets`
(`app/market_engine/buckets.py:43-44,73-74`) and the daily→session canonicalizer
(`app/market_engine/historical/session_candles.py:50-56`) anchor **every** trading date
to the single `SessionSchedule.regular_open/regular_close`; `HistoricalRangePlanner`
(`app/market_engine/historical/service.py`) derives `_candles_per_session` from that one
schedule. No per-date schedule lookup exists.

---

## Session-hours analysis (§8) — code evidence

| Path | Depends on | Verdict |
|------|-----------|---------|
| A. `HistoricalCalendarWindow.previous_trading_day(s)` | trading-*date* membership only (`is_trading_day`) | **Date-only.** An OPEN override makes it correct. |
| B. `HistoricalRangePlanner._sessions_needed` (intraday) | fixed `candles_per_session` from the **global** regular schedule | **Assumes standard hours.** A short special session has fewer candles. |
| C. `canonicalize_session_candle` / `session_buckets` | global `regular_open/close` bounds | **Assumes standard hours.** Special-session bounds would be mis-stamped. |
| D. Historical completeness | expected buckets from global hours | A shortened session would be judged incomplete/invalid under regular hours. |
| E. Session (whole-day) timeframe | trading-date membership + regular-bounds identity; **OHLCV preserved from the daily bar** | Value-correct with an OPEN override; identity label is the canonical regular window (consistent with the live engine, which uses the same global schedule). |
| F. Warmup | selection vs. intraday expectation | A special date can be **selected** correctly while its **intraday** candle expectations are wrong. |

### Outcome — **H3 (PARTIAL AUTHORITY)**
Date-level and whole-session (daily) resolution **can** be authoritative with an OPEN
override (the previous-session OHLCV a session/daily strategy consumes is value-correct
and reconciles consistently with the live engine's identical global-schedule bucketing).
**Intraday** requirements whose window includes a non-standard-hours session **cannot** be
authoritative without per-date session-hours metadata and must **fail closed**. H2 is
rejected as over-requiring (it would force intraday-hours provisioning even for
session/daily-only strategies); H1 is rejected because intraday would be silently wrong.

> ⚠️ Once an OPEN override makes a special date *selectable*, intraday planning would
> otherwise bucket it with regular hours and be **silently wrong**. The model MUST gate
> this: an OPEN-override date lacking session-hours metadata is resolvable for the
> session/daily timeframe but **fails closed for any intraday timeframe** (M11/M16).

---

## Decisions

- **M1 — OPEN overrides required.** Authoritative classification requires an explicit,
  broker-neutral set of dates that are OPEN despite being weekends (or otherwise).
- **M2 — OPEN representation.** A future `TradingCalendar` gains an immutable,
  deterministic, date-based `open_sessions: frozenset[date]` (name illustrative),
  provider-neutral and strategy-neutral, all dates constrained to lie within
  `CalendarCoverage`.
- **M3 — CLOSED representation/terminology.** The canonical future model should use a
  general **`closed_dates`** term (an exchange closure need not be a "holiday").
  Production field renames are **out of scope this phase**; only the future canonical
  terminology is governed.
- **M4 — OPEN/CLOSED conflict.** A date present in both the OPEN and CLOSED sets is
  **invalid calendar data → fail fast at dataset validation.** No arbitrary precedence.
- **M5 — Classification precedence.** Governed order:
  `OPEN override → else weekend ⇒ CLOSED → else CLOSED date ⇒ CLOSED → else OPEN`.
  Verified against normal weekdays/weekends, weekday closures, weekend-open sessions,
  and coverage boundaries.
- **M6 — CalendarCoverage relationship.** Every exception date must lie **inside**
  `CalendarCoverage`. Overrides never extend coverage; dates outside coverage remain
  non-authoritative and fail closed (`OutsideCalendarCoverageError`).
- **M7 — Session-hours metadata: required only for intraday over special sessions.**
- **M8 — H3 (PARTIAL AUTHORITY)** selected (see analysis above).
- **M9 — Session-hours override model (only where H3 requires it).** A future minimal,
  broker-neutral per-date `TradingSessionOverride(trading_date, live_start, live_end)`
  (fields illustrative — only what reconstruction needs). Do **not** model pre-open,
  auction, or closing phases unless historical reconstruction demonstrably needs them.
- **M10 — Effective schedule rule.** `effective_schedule(trading_date)` = the date's
  override if present, else the canonical default `SessionSchedule`. A special date MUST
  NOT mutate the global/default schedule; **no leakage** to adjacent sessions.
- **M11 — Missing timing metadata.** If a special OPEN date lacks required session-hours
  metadata, the **affected intraday historical requirement fails closed** — the normal
  NSE schedule is **not** applied merely because the date is known OPEN.
- **M12 — Dataset completeness.** "Authoritative for the window" means the dataset can
  represent, within `CalendarCoverage`: default weekday trading; default weekend closure;
  explicit weekday closures; explicit exceptional OPEN dates; and (for intraday
  authority) per-date session-hours for those OPEN dates. A model that cannot express
  known exceptions cannot claim completeness.
- **M13 — Validation rules.** `coverage_start ≤ coverage_end`; all closed/open/override
  dates within coverage; unique dates; no OPEN∩CLOSED conflict; deterministic ISO
  parsing; a `TradingSessionOverride` only for a date in the OPEN set with
  `live_start < live_end`.
- **M14 — HistoricalCalendarWindow behavior.** Unchanged except that `previous_trading_day(s)`
  now includes exceptional OPEN dates via the extended `is_trading_day`.
- **M15 — HistoricalRangePlanner behavior.** Session-timeframe demand resolves over the
  corrected trading-date set. Intraday demand consults `effective_schedule`; if any
  in-window special date lacks hours metadata, that intraday requirement fails closed.
- **M16 — Intraday historical behavior.** Authoritative only when every special session
  in the resolved window has session-hours metadata; otherwise fail closed (never
  silently regular-houred).
- **M17 — Session-timeframe behavior.** Authoritative with the OPEN override; OHLCV is
  preserved and identity remains consistent with the live engine's global-schedule
  bucketing.
- **M18 — Current-day isolation.** Preserved: `supports_current_day=False`;
  current-day withheld; `CURRENT_DAY_RECONCILIATION_GUARANTEE = NOT PROVEN`. Completed
  sessions only.
- **M19 — Provider neutrality & replay determinism.** No `Dhan`, `security_id`,
  `exchange_segment`, REST/WebSocket, or packet codes in the calendar model. All
  classification is pure/date-driven; no wall-clock; replay records the calendar
  dataset/version.
- **M20 — Provisioning contract.** The subsequent **ADR-011-DATA-R1** slice provisions the
  authoritative NSE dataset (closed dates, open-session dates, and per-date session-hours
  for those open dates), validated per M13, entirely within `CalendarCoverage`.

## Fail-closed states (kept distinct — M-level)
1. No authoritative dataset → `UnavailableHistoricalWarmup`.
2. Date outside `CalendarCoverage` → `OutsideCalendarCoverageError`.
3. OPEN∩CLOSED conflict → invalid calendar data / fail fast.
4. Special OPEN date missing required session-hours for an intraday requirement → that
   requirement fails closed.
5. Provider historical failure → provider/source error (distinct).
6. Current-day request → withheld.

## Related (out of scope, noted)
The **live** engine shares the same global-schedule assumption (ADR-010 scope); a
weekend-open live session would mis-bucket identically. This addendum governs the
**historical** path only; any live-session-hours handling is a separate future decision.

---

## Future implementation contract (documented, not implemented)
Extend `TradingCalendar` with `open_sessions` (and future canonical `closed_dates`
naming); reject OPEN∩CLOSED conflicts; update `is_trading_day` per M5; ensure
`previous_trading_day(s)` includes OPEN overrides; add an `effective_schedule` lookup and
per-date `TradingSessionOverride` for intraday; make intraday planning/reconstruction
consult it and fail closed on missing timing (M11/M16). Then **ADR-011-DATA-R1**
(dataset provisioning), then **ADR-011-IMPL** (composition wiring). Backward-compatible
where safe (empty override sets ⇒ today's behavior).

## Future test matrix
- **Classification:** normal weekday→open; normal weekend→closed; explicit closed
  weekday→closed; explicit open weekend→open; OPEN∩CLOSED conflict→rejected; outside
  coverage→fail closed.
- **Historical window:** `previous_trading_day` includes a weekend-open date;
  `previous_trading_days` preserves oldest→newest order; explicit weekday closure
  skipped; exact coverage boundary resolves.
- **H3 intraday/session-hours:** date-specific schedule selected for a special date;
  ordinary date uses default schedule; special shortened-session bucket count correct;
  missing special schedule → intraday requirement fails closed; no schedule leakage
  between dates; session/daily requirement over the same window still resolves.
- **Isolation:** `supports_current_day=False`; `staged_observation_verified=False`;
  `tick_aggregate_verified=False`; provider architecture guards green.

## Consequences
**Positive.** The calendar model can express real NSE session exceptions; date/session
authority is achievable; intraday authority across special sessions is explicitly gated,
never silently wrong. **Negative/accepted.** Intraday warmup over special sessions stays
fail-closed until session-hours are provisioned. **Neutral.** No production code changes
here; the extension and dataset are later slices.
