# ADR-009 — Operational Addendum: Session-Statistics REST Refresh Execution by Market Phase

| Field | Value |
|-------|-------|
| **Type** | Operational addendum, subordinate to ADR-009 (Accepted) |
| **Status** | Accepted |
| **Date** | 2026-08-12 |
| **Subordinate to** | [ADR-009](ADR-009-rest-backed-authoritative-session-statistics.md) (REST-Backed Authoritative Current-Session Statistics) |
| **Related** | [ADR-010](ADR-010-live-market-runtime-composition-and-managed-ingestion-lifecycle.md), ADR-007 (requirement lifecycle) |

> ADR-009 is Accepted and immutable. This addendum **does not change** ADR-009's authority
> model, freshness model, source precedence, staging, one-version semantics, two-state
> quality, or the provider-evidence gate. It only **operationalizes D10** ("refresh is
> activated only when required") by deciding **when** the managed refresh driver
> (P4.6E7) may call `refresh_if_due` as a function of the canonical market phase. Nothing
> here authorizes any `SessionStatisticsAuthority` bit.

## Context

The refresh driver deferred to P4.6E7 must decide, each cycle, whether to poll the REST
session-statistics source. ADR-009 defines *what a refresh yields and whether it may be
authoritative* per phase, but not *whether the poll executes* per phase. P4.6E7 flagged
this gap. The Market Engine primitive advances/establishes `SessionStatistics` **only
during `LIVE_SESSION`** (`app/market_engine/session_statistics.py`: a non-`LIVE_SESSION`
phase retains the prior snapshot and never progresses — confirmed by the P4.6B update
tests for `PRE_OPEN`, `OPENING_AUCTION`, `CLOSING_SESSION`, `MARKET_CLOSED`,
`EMERGENCY_HALT`, `HOLIDAY`). A REST observation staged outside `LIVE_SESSION` is therefore
a no-op at the engine — so polling outside `LIVE_SESSION` is wasteful, never corrective.

## Decision — refresh execution by phase

| Market phase | Refresh poll? | Why |
|---|---|---|
| `PRE_OPEN` | **NO** | No valid regular-session statistics exist; pre-open indicative values are never promoted (ADR-009). A staged observation could not establish stats. |
| `OPENING_AUCTION` | **NO** | Same as pre-open; the regular session has not begun. |
| `LIVE_SESSION` | **YES** — only when `SESSION_STATISTICS` demand is active **and** the coordinator's cadence says due | The only phase in which the engine establishes/advances authoritative regular-session statistics. |
| `CLOSING_SESSION` | **NO** | The engine retains but never advances stats after the regular session (P4.6B); a REST observation fetched here would be ignored. No "final refresh" is added because it would be a deliberate no-op. |
| `MARKET_CLOSED` | **NO** | Unusable; no session progression. |
| `HOLIDAY` | **NO** | Unusable; no trading session. |
| `EMERGENCY_HALT` | **NO** (suspend while halted) | The engine retains last-known stats and does not treat halt activity as progression (P4.6B); provider activity must not redefine session state. The driver resumes only when canonical classification returns `LIVE_SESSION`. |

Net: **refresh executes only during `LIVE_SESSION`, and only under active demand and
cadence.** All other phases: no poll.

## Driver gating order (normative)

1. Obtain an explicit reference instant from the runtime `Clock` (`SystemClock` in
   production; `ManualClock` in tests).
2. Classify the canonical session/phase via the Market Engine `MarketSessionClassifier`
   (the driver does **not** classify sessions itself, and the refresh coordinator does
   **not** classify sessions).
3. If phase ≠ `LIVE_SESSION` → **no** refresh call.
4. If no active `SESSION_STATISTICS` demand (empty `FactRequirementRegistry`) → **no**
   refresh call.
5. Otherwise call `refresh_if_due(reference=…, trading_date=…)`.

## Trading date

The driver supplies the canonical `trading_date` from the **same** session/calendar
semantics the Market Engine uses (`MarketSessionClassifier` / `SessionSchedule` /
`TradingCalendar`). It never uses `date.today()`, `datetime.now().date()`, or a
provider-local date.

## Explicitly unchanged

- **Freshness:** unchanged (ADR-009 / P4.6E5). Authority ≠ freshness; not polling in a
  phase does not alter `as_of` or readiness semantics.
- **Authority:** unchanged and **disabled**. This addendum authorizes no
  `staged_observation_verified=True` or `tick_aggregate_verified=True`; both sources remain
  gated on their own verification (E6/E6A; E6B evidence still insufficient).
- **Source precedence, staging, one-version, two-state quality:** unchanged.
- **No new provider semantics** are claimed and **no current-day historical** change is made.
