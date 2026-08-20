# ADR-011 — Historical Trading-Calendar Authority Window

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-13 |
| **Deciders** | Market-Engine Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Complements** | ADR-004 (NSE cash-equity V1 universe), ADR-006 (candle completeness / feed continuity), ADR-007 (strategy lifecycle & requirements), ADR-010 (live-market runtime composition) |
| **Refines** | `docs/06_MARKET_ENGINE.md` (§8 sessions, §14.2 fail-closed history) |
| **Related** | ADR-008/009 (session statistics — *independent* authority, see D16) |

---

## Context

The Market Engine's completed-session historical path resolves "the *N* previous
trading days before a reference" by walking backward through a broker-neutral
`TradingCalendar` (weekends + configured NSE holidays). To resolve a date the engine
must *know* whether that date was a trading day. The engine must never assume an
unknown historical date was a trading day merely because it is absent from an
incomplete holiday set — doing so would silently fabricate history.

The code already models this with a distinct type, `CalendarCoverage(start_date,
end_date)` (`app/market_engine/historical/calendar_window.py`): an **inclusive date
interval over which the `TradingCalendar` classification is asserted authoritative**.
Outside it, `HistoricalCalendarWindow.previous_trading_day(s)` raises
`OutsideCalendarCoverageError` and historical planning fails closed.

Three concepts are already **distinct** in the code and must remain so:

1. **`HistoricalRequirement(timeframe, lookback)`** — strategy-owned *demand*, where
   `lookback` is a candle (or, for the session timeframe, a session) count.
2. **`HistoricalRangePlanner`** — infrastructure that *translates* demand into the
   number of completed sessions required and then into a completed-session date range
   (`_sessions_needed`: session timeframe ⇒ lookback is sessions; intraday ⇒
   `ceil(lookback / candles_per_session) + session_margin`).
3. **`CalendarCoverage`** — the *validity boundary* of canonical calendar knowledge.

A prior governance attempt (`ADR-011-GOV`) mis-modelled `CalendarCoverage` as a
retention/lookback horizon whose scalar value an owner must pick. Repository
inspection disproved that premise. This ADR governs the **actual** model.

Two production gaps surfaced during inspection and are governed here rather than
solved in code:

- **G-A — no coverage window in production.** `compose_market_runtime` passes
  `calendar_coverage=None`, so the historical seam is `UnavailableHistoricalWarmup`
  (fail-closed). There is no `calendar_coverage_*` setting.
- **G-B — the holiday-data shape is ambiguous.** `settings.nse_holidays: str = ""`
  cannot distinguish *"no holidays exist in the window"* (complete-and-none) from
  *"no holiday dataset was provisioned"* (unprovisioned). `TradingCalendar` treats an
  empty set as "every weekday is a trading day" — which is unsafe to assert over any
  real historical window.

---

## Decision

### D1 — Definition
`CalendarCoverage` is the **inclusive `(start_date, end_date)` calendar-date interval
over which canonical `TradingCalendar` knowledge (weekends + NSE holidays) is asserted
complete and authoritative.** It is the validity boundary of calendar knowledge — not
demand, not retention, not a lookback count.

### D2 — Purpose (fail-closed)
Its purpose is to prevent the planner from assuming an *unknown* historical date was a
trading day merely because that date is absent from an incomplete holiday set. Outside
the authoritative window the engine **fails closed** (`OutsideCalendarCoverageError`),
never "not-a-listed-holiday ⇒ trading day."

### D3 — Units
Explicit calendar dates: `start_date` and `end_date`, **inclusive** both ends. This
governance is **not** convertible to a scalar number of days or sessions.

### D4 — Holiday-dataset coupling
A `CalendarCoverage` window is **valid only if the canonical holiday dataset is complete
for the entire interval**. A configured date window without correspondingly complete
holiday data must **not** be treated as authoritative. Coverage asserts calendar
*completeness*; it is meaningless without the data that backs it.

### D5 — Holiday source of truth
Today the only holiday input is `settings.nse_holidays` (a validated comma-separated
ISO-date string, default empty), consumed identically by `MarketSessionClassifier`
and `_schedule_and_calendar`. There is **no** governed provisioning process, packaged
dataset, exchange-calendar service, or DB-backed calendar. Therefore:

> **HOLIDAY_DATA_PROVISIONING = OWNER / DATA DECISION REQUIRED.**

Until an authoritative NSE holiday dataset and its matching window are provisioned,
production historical calendar authority remains **fail-closed** (D12). Provider-
specific holiday semantics, security IDs, or exchange segments must never enter
`TradingCalendar` or `CalendarCoverage` (D15).

### D6 — Weekend semantics
Weekends (`date.weekday()` in `{Sat, Sun}`) remain deterministically non-trading from
canonical calendar rules, independent of the holiday dataset.

### D7 — Completed-session resolution
Historical planning continues to walk backward over **completed** trading sessions via
the `TradingCalendar`. The current (possibly-incomplete) session is never fabricated as
completed history.

### D8 — Current-day isolation
This ADR does **not** authorize current-day historical reconciliation. Preserved exactly:
`supports_current_day=False`; current-day intervals `WITHHELD`;
`CURRENT_DAY_RECONCILIATION_GUARANTEE = NOT PROVEN` (ADR-009, docs/06).

### D9 — Relationship to `HistoricalRequirement`
`HistoricalRequirement` determines *demand*; `CalendarCoverage` determines whether the
canonical calendar has enough *authoritative history* to resolve that demand. The two
are **not** merged. Coverage neither expresses nor caps demand; it bounds the search
domain.

### D10 — Demand exceeding coverage
If planner resolution would reach a date **before** `start_date`, resolution fails
closed (`OutsideCalendarCoverageError`). **No silent truncation**, and a reduced
lookback must never masquerade as satisfied readiness/warmup.

### D11 — Exact-boundary semantics
If a required completed-session date falls exactly on `start_date`, and that date is
inside coverage, resolution is **permitted**. Any date strictly earlier is not. (Proven
by `test_historical_calendar_window.py`: `start_date` accepted; one day earlier raises.)

### D12 — Missing coverage
If production has a history-consuming strategy but no authoritative `CalendarCoverage`,
historical warmup remains **unavailable and fail-closed**. This ratifies the existing
`UnavailableHistoricalWarmup` port (`app/services/strategy_requirements_wiring.py`).

### D13 — Invalid coverage
`end_date < start_date` **fails fast at construction** (existing `__post_init__`
`ValueError`). Boundaries are never normalized or swapped automatically.

### D14 — Zero historical demand
No historical warmup/provider call occurs merely because a `CalendarCoverage` exists.
Zero historical demand remains zero work: the coordinator invokes warmup only when a
strategy actually declares a `HistoricalRequirement`.

### D15 — Provider neutrality
`CalendarCoverage`, `TradingCalendar`, `HistoricalRequirement`, and all planning remain
broker-neutral. Dhan-specific security IDs, exchange segments, API retention, or
provider calendars must not leak into the Market-Engine contract.

### D16 — Authority isolation
This ADR does not touch session-statistics authority. `staged_observation_verified` and
`tick_aggregate_verified` remain `False`. **Calendar authority ("was this a trading
day?") is unrelated to current-session-statistics authority ("is this OHLC
authoritative?").**

### D17 — Coverage-window extent (sizing principle, not a number)
No arbitrary window is chosen (not 30/60/90/365 days, not 100 sessions). The **sizing
principle**: the minimum required `start_date` is eventually *derived* from
`max(effective HistoricalRequirement) → HistoricalRangePlanner session demand →
earliest required completed-session date`, plus the planner's existing deterministic
`session_margin`. A provisioned window **may** extend further back than current demand,
provided its holiday dataset is authoritative for that period (D4). No mandatory maximum
window is invented.

### D18 — No-strategy state
With **zero** history-consuming strategies there is no demand-derived minimum window.
Zero current demand is therefore **not** a justification for inventing an arbitrary
coverage `start_date`.

### D19 — Future strategy onboarding
When the first history-consuming strategy specification is introduced, its
`HistoricalRequirement` supplies the demand that determines whether the provisioned
`CalendarCoverage` is sufficient. If insufficient, the strategy's START fails
readiness/warmup **closed** (D10). The calendar is never dynamically stretched to
"know" dates outside its authoritative window.

### D20 — Operational configuration ownership
The holiday dataset, `coverage.start_date`, and `coverage.end_date` are **operational
data/configuration artifacts**. A configuration change must **never** silently assert
authority over dates for which holiday data was not provisioned (D4). Changing them is
an operational data change subject to the D4 completeness rule, not an ad-hoc runtime
tweak.

### D21 — Restart semantics
A restart must rebuild the *same* canonical coverage and calendar from governed
configuration/data. Coverage must not depend on the process start date or wall clock.

### D22 — Determinism
No `date.today()` / `datetime.now()` inside historical planning. Reference and session
dates remain explicit and canonical (the `calendar_window` module is already pure).

### D23 — Observability (no collapsed failure modes)
These four states must remain **distinguishable** and must never collapse into a
fabricated `READY`/`SATISFIED`:
1. historical requirement unavailable — no authoritative calendar window
   (`HistoricalWarmupUnavailableError`);
2. requested history extends outside calendar coverage
   (`OutsideCalendarCoverageError`);
3. historical provider/source failure (a distinct provider error);
4. current-day withheld (D8).

### D24 — Future extension
A later governed holiday-data service may **extend** `CalendarCoverage` by adding
authoritative dates — but only additively, and only when the holiday/session dataset for
the newly covered dates is proven complete (D4).

### D25 — Concrete-strategy boundary
This ADR specifies **no** concrete strategy (no Narrow CPR, no Open=High/Open=Low). It
only removes an infrastructure ambiguity that a future history-consuming strategy spec
may depend on.

---

## Calendar-authority source-of-truth decision

Options evaluated against existing architecture:

| Option | Verdict |
|--------|---------|
| A. Explicit config (`calendar_coverage_start/end`, `nse_holidays`) | **Chosen mechanism** — extends the existing settings-driven, remote-free model. Requires the D4 completeness guarantee and the G-B provisioned/complete-vs-empty distinction. |
| B. Packaged canonical NSE calendar dataset | Viable future provisioning source; none exists today. |
| C. Governed exchange-calendar provider/service | Out of scope; would need its own ADR; must stay broker-neutral (D15). |
| D. DB-backed canonical trading-calendar data | Viable future source; none exists today. |
| E. Window derived from an authoritative holiday dataset | The validity rule (D4), not a standalone source. |

**Decision:** govern the mechanism as settings/data-driven (Option A shape), backed by
whichever provisioning source (B/D) a follow-up chooses. Because no authoritative
dataset or window is provisioned today:

> **PRODUCTION HISTORICAL CALENDAR AUTHORITY = UNAVAILABLE UNTIL AUTHORITATIVE HOLIDAY
> DATA + WINDOW ARE PROVISIONED** — an accepted fail-closed operational state (D12).

## Provider-retention distinction

`CalendarCoverage` answers **"was this date a trading day?"** — it does **not** assert
the provider can return candles for it. Provider historical availability ("can the
provider return the requested candles?") is independent: historical warmup must still
fail on its own when the provider cannot satisfy requested history, separately from any
calendar-coverage failure (D23 item 3).

## Production data/window provisioning status

- Calendar-authority **model**: **GOVERNED** (this ADR).
- Production **holiday dataset**: **NOT PROVISIONED** (`nse_holidays` empty; no
  complete-vs-unprovisioned distinction — G-B).
- Production **coverage window**: **NOT PROVISIONED** (no `calendar_coverage_*`
  setting; composition passes `None` — G-A).
- Runtime behaviour meanwhile: **fail-closed** via `UnavailableHistoricalWarmup` (D12).

---

## Future implementation contract (documented, not implemented here)

A subsequent slice (**ADR-011-DATA / ADR-011-IMPL**) will:
- add an explicit coverage source — `calendar_coverage_start` / `calendar_coverage_end`
  settings or a canonical calendar-data object;
- represent the **provisioned-vs-empty** holiday distinction safely (G-B) so an empty
  string can never silently assert authority;
- provision an authoritative NSE holiday dataset for the intended window;
- construct `CalendarCoverage` only when dataset and dates satisfy D4;
- wire real historical warmup into `compose_market_runtime` only when calendar authority
  exists, preserving `UnavailableHistoricalWarmup` otherwise;
- size the window per D17 once the first history-consuming strategy exists.

The exact files/seam are deferred to that slice; the cleanest seam is the existing
`calendar_coverage` parameter of `compose_market_runtime` plus new validated settings.

## Future test matrix (for the implementation slice)

- **Calendar model:** valid inclusive coverage; inverted range rejected; `start_date`
  boundary accepted; one day before `start_date` rejected.
- **Calendar semantics:** weekends skipped; listed holidays skipped; ordinary weekdays
  selected; no trading-day assumption outside coverage.
- **Historical planning:** requirement entirely within coverage; exact-boundary
  requirement; requirement exceeding coverage → fail closed; no silent lookback
  truncation; session-timeframe demand; intraday-timeframe demand; deterministic
  `session_margin` application.
- **Operational state:** missing calendar data → historical warmup unavailable;
  unprovisioned/empty holiday dataset must not imply calendar authority; zero historical
  demand → zero provider calls; provider failure distinct from calendar-coverage failure.
- **Isolation:** `supports_current_day=False`; current-day withheld;
  `staged_observation_verified=False`; `tick_aggregate_verified=False`; no Dhan type in
  the Market Engine; architecture guards remain green.

---

## Consequences

**Positive.** The historical calendar-authority model is unambiguous and matches the
code: demand, translation, and calendar-validity are three separate concerns; every
failure mode is distinct and fail-closed; the unsafe "empty holidays ⇒ all weekdays
trade over any window" assumption is now an explicitly governed provisioning gap rather
than a silent behaviour.

**Negative / accepted.** Production historical warmup stays disabled until an
authoritative holiday dataset and coverage window are provisioned (D5, D12) — a
deliberate fail-closed posture, not a regression.

**Neutral.** No production code changes in this ADR; it ratifies existing behaviour and
governs the provisioning that unblocks it.

## Acceptance checklist

- [x] `CalendarCoverage` governed as an inclusive calendar-authority date window (D1–D3).
- [x] Holiday-dataset completeness rule stated (D4); source-of-truth decided; provisioning
  marked OWNER/DATA DECISION REQUIRED (D5).
- [x] Demand / translation / calendar-authority kept distinct (D9); fail-closed on
  exceed and on missing/invalid coverage ratified (D10, D12, D13).
- [x] Current-day isolation and session-statistics-authority isolation preserved (D8, D16).
- [x] Determinism, provider-neutrality, provider-retention distinction stated (D15, D22).
- [x] Observability failure modes kept distinguishable (D23).
- [x] No concrete strategy specified; no arbitrary window invented (D17, D18, D25).
- [x] Production data/window provisioning status recorded as NOT PROVISIONED (fail-closed).
