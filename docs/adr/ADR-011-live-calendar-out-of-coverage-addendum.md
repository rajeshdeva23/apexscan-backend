# ADR-011 Addendum — Live Out-of-Coverage Calendar Classification Contract

| Field | Value |
|-------|-------|
| **Type** | Subordinate governance addendum (not a numbered ADR) |
| **Subordinate to** | ADR-011 — Historical Trading-Calendar Authority Window |
| **Refines** | ADR-011 live-calendar-source governance (Outcome B) — supplies the out-of-coverage mechanism it deferred |
| **Related** | ADR-010 (runtime lifecycle), ADR-011 multi-interval addendum, ADR-011 NSE calendar evidence record |
| **Status** | Accepted (decision + implementation contract); **implementation DEFERRED** to ADR-011-LIVE-CALENDAR-IMPL-R1 |
| **Date** | 2026-08-16 |
| **Deciders** | Market-Engine Architecture |
| **Decision** | **Option B — add `MarketState.CALENDAR_UNAVAILABLE`**; the classifier owns `CalendarCoverage` and returns it when a date is outside coverage. |

---

## Context

ADR-011-LIVE-CALENDAR-IMPL correctly STOPPED: unifying live date-level classification onto the
dataset requires an explicit `OUTSIDE_AUTHORITATIVE_COVERAGE` representation, and the current
contract (`MarketState`, `SessionContext`, `MarketSessionClassifier.classify`) cannot express it.
This addendum governs that missing contract. The gap is confirmed by inspection: `MarketState` =
{PRE_OPEN, OPENING_AUCTION, LIVE_SESSION, CLOSING_SESSION, MARKET_CLOSED, HOLIDAY, EMERGENCY_HALT} —
no out-of-coverage value; `SessionContext` = {trading_date, market_state, exchange_timezone} — no
coverage flag; the classifier has no coverage concept.

## Consumer-impact matrix (from repository inspection)

| Consumer | How it reads classification | Behaviour on a new non-LIVE_SESSION state |
|----------|-----------------------------|-------------------------------------------|
| `SessionStatisticsRefreshDriver._cycle` | `if state is not LIVE_SESSION: skip` | auto fail-closed (skips) |
| `CandleEngine` | `if session_state is not LIVE_SESSION: …` gate | auto fail-closed |
| `session_statistics` staging | `state is (not) LIVE_SESSION` gate | auto fail-closed |
| `strategy_manager/triggers` | `previous.market_state != current.market_state` | benign (transition on change) |
| `MarketSessionClassifier` | sole producer | — |

**No consumer acts specifically on `MARKET_CLOSED`/`HOLIDAY`, and none does an exhaustive
`match` requiring every value.** Every behaviour-bearing gate is a *positive* `is LIVE_SESSION`
check, so a new non-`LIVE_SESSION` state fails them all closed with **zero forget-to-check risk**.

## Option assessment

- **Option A — `SessionContext.calendar_authoritative: bool`.** *Rejected.* Hits §7-A5: there is no
  honest `MarketState` to pair with `False` (reusing `MARKET_CLOSED`/`HOLIDAY` falsely asserts
  "known closed"; leaving a phase like `LIVE_SESSION` is the contradictory combination §6/§9 forbid).
  It also reintroduces the highest-risk failure mode (§5): a consumer reading only `market_state ==
  LIVE_SESSION` and ignoring the bool would act on a garbage/stale state. Dual fields can disagree.
- **Option B — `MarketState.CALENDAR_UNAVAILABLE`.** *Selected.* Single honest source of truth; no
  contradictory (state, bool) combination possible; every existing `is LIVE_SESSION` gate
  fail-closes by construction; downstream semantics are fully determinable (all gates are positive
  LIVE_SESSION checks) → STOP #2 not triggered. Ripple: any future exhaustive `match` must add a
  fail-closed branch (none exists today).
- **Option C — `classify` raises `OutsideCalendarCoverageError`.** *Rejected.* Turns a
  per-tick/per-poll classification into an exception path every caller must catch; loses the
  state-valued result the `triggers` transition detector consumes; more invasive contract change.
- **Option D — startup/lifecycle refusal only.** *Rejected alone.* Fails the cross-midnight case
  (§8): a runtime started on 2026-12-31 that survives into 2027-01-01 keeps classifying via the
  stale calendar. A startup check is insufficient because classification happens continuously. It
  MAY be added later as optional defense-in-depth, but B (enforced at classify time) is required.

## Decisions LC1–LC20

- **LC1 — Calendar authority.** Exchange trading-day + phase classification is authoritative only
  for dates within the dataset's `CalendarCoverage`.
- **LC2 — Outside coverage.** The system does not know the trading status; it is represented
  explicitly as `CALENDAR_UNAVAILABLE` and treated fail-closed. Never inferred as trading, closed,
  or holiday.
- **LC3 — Chosen representation.** Option B: new `MarketState.CALENDAR_UNAVAILABLE`.
- **LC4 — Rejections.** A (A5 dead-end + dual-field forget-risk), C (invasive raise-contract, loses
  state for transitions), D-alone (cross-midnight failure). See above.
- **LC5 — Classifier responsibility.** `MarketSessionClassifier` owns a `CalendarCoverage` and, in
  `classify`/`_state_for`, checks `trading_date ∈ coverage` **first**; outside → `CALENDAR_UNAVAILABLE`
  (before the `is_trading_day`/halt logic). Callers never decide authority.
- **LC6 — CalendarCoverage ownership.** Injected into the classifier at composition from the resolved
  `TradingCalendarDataset`; broker-neutral; not broker-supplied.
- **LC7 — SessionContext semantics.** Shape unchanged (no new field — avoids the dual-field
  contradiction); `market_state` may now be `CALENDAR_UNAVAILABLE`.
- **LC8 — MarketState semantics.** `CALENDAR_UNAVAILABLE` = "trading status not authoritatively known
  (date outside coverage)". It is neither a trading state nor an authoritative closed state; it is
  mutually exclusive with all phase/closed states.
- **LC9 — Contradiction prevention.** A single enum value cannot contradict itself; the classifier is
  the sole producer; `CALENDAR_UNAVAILABLE` is returned instead of (never alongside) any phase/closed
  state. No `(state, flag)` pair exists to disagree.
- **LC10 — TickEngine.** Gates on `is LIVE_SESSION`; `CALENDAR_UNAVAILABLE ≠ LIVE_SESSION` ⇒ no
  authoritative live-session progression, no strategy-visible live advance, no fabricated
  trading/holiday classification — fail-closed. (IMPL must confirm no path treats non-LIVE as
  authoritative closed.)
- **LC11 — SessionStatisticsRefreshDriver.** `if state is not LIVE_SESSION: skip` ⇒ `CALENDAR_UNAVAILABLE`
  skips; no refresh progression; no provider-semantic implication; fail-closed.
- **LC12 — Other consumers.** `session_statistics` staging (LIVE_SESSION gate → skip), `CandleEngine`
  (LIVE_SESSION gate → skip), `triggers` (`!=` → benign transition). No new branches required; IMPL
  must still scan for any exhaustive `match` and add a fail-closed arm if found.
- **LC13 — Cross-midnight.** Enforced at classify time using the instant's `trading_date` vs coverage,
  not at startup → correct across 2026→2027 with no restart (§8).
- **LC14 — Missing/invalid dataset vs outside coverage.** Distinct: missing/invalid dataset →
  composition/startup fail-closed (ADR-011-IMPL: `UnavailableHistoricalWarmup` + no live calendar
  authority); date outside coverage → runtime `CALENDAR_UNAVAILABLE`. Not collapsed.
- **LC15 — settings.nse_holidays.** MUST NOT be a fallback when out of coverage; no
  `if outside: use settings` branch. Legacy/non-authoritative for the dataset-enabled path; removal
  deferred to a future cleanup.
- **LC16 — Exceptional OPEN.** Within coverage, 2026-02-01 and 2026-11-08 classify as trading dates
  (date-level, via the dataset calendar's `open_sessions`).
- **LC17 — Live session-hours isolation.** Date-level only. Live `CandleEngine` keeps the default
  `SessionSchedule`; no exceptional live intraday hours; Muhurat intraday timing stays NOT_PROVEN.
- **LC18 — Historical/current-day isolation.** `supports_current_day=False` unchanged; historical
  `CalendarCoverage`/`OutsideCalendarCoverageError` unchanged; live date authority ≠ current-day
  historical authority.
- **LC19 — Session-statistics authority isolation.** `staged_observation_verified`/
  `tick_aggregate_verified` stay False; calendar authority proves nothing about Dhan O/H/L; P4.6E6C
  remains separate.
- **LC20 — Provider-neutrality / determinism / lifecycle.** No Dhan types in the contract; `classify`
  stays deterministic (instant-driven, no wall-clock); no new runtime task; ADR-010 lifecycle
  unchanged (coverage flows through the existing composition seam).

## Implementation contract (for ADR-011-LIVE-CALENDAR-IMPL-R1)
- **Domain:** add `MarketState.CALENDAR_UNAVAILABLE` (`context.py`).
- **Classifier (`session.py`):** `MarketSessionClassifier.__init__` gains `coverage: CalendarCoverage`;
  `_state_for` returns `CALENDAR_UNAVAILABLE` when `trading_date ∉ coverage` (checked before
  `is_trading_day`/halt). Decide the `from_settings`/disabled-runtime path (no dataset): prefer an
  explicit "everything CALENDAR_UNAVAILABLE" or retain legacy settings behaviour only for the
  disabled/no-live path — the disabled runtime carries no live data, so it is out of the live-trading
  authority scope.
- **Composition (`market_runtime.py`, `dhan_runtime_composition.py`):** build the live classifier from
  the dataset `TradingCalendar` + dataset `CalendarCoverage` when provisioned.
- **Consumers:** LIVE_SESSION gates need no change; scan for any exhaustive `match` and add a
  fail-closed arm if present.
- **Files expected to change:** `context.py`, `session.py`, `market_runtime.py`,
  `dhan_runtime_composition.py`, plus tests; possibly a driver/candle-engine test to include the new
  state.
- **Files that MUST NOT change:** historical algorithms/coverage/`OutsideCalendarCoverageError`;
  `CandleEngine` bucketing/`SessionSchedule` hours; authority config; current-day handling.

## Future test matrix
ordinary weekday in coverage → trading; ordinary weekend in coverage → closed; 2026-01-26 → closed;
2026-01-15 → closed; 2026-02-01 Sunday → trading; 2026-11-08 Sunday → trading; date immediately
before coverage (2025-12-31) → `CALENDAR_UNAVAILABLE`; immediately after (2027-01-01) →
`CALENDAR_UNAVAILABLE`; runtime crossing 2026→2027 without restart → next `classify` returns
`CALENDAR_UNAVAILABLE`; TickEngine on `CALENDAR_UNAVAILABLE` → no live progression; refresh driver on
`CALENDAR_UNAVAILABLE` → skip; no `settings.nse_holidays` fallback out of coverage; Muhurat intraday
timing unavailable; historical behaviour unchanged; `supports_current_day=False`; authority bits
False; Market Engine Dhan-free; no new runtime task; single EventBus/InstrumentStateRegistry intact.

## Files changed
Docs-only: this addendum + the README index row. No `app/`/`tests/` change.

## Next slice
**ADR-011-LIVE-CALENDAR-IMPL-R1** — implement Option B per the contract above, then the calendar
monitor (independent). Historical-only strategy work (Narrow CPR) remains unblocked.
