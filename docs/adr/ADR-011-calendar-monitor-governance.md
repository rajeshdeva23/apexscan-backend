# ADR-011 Addendum — Secondary Calendar-Monitor Governance (Dhan public holiday page)

| Field | Value |
|-------|-------|
| **Type** | Subordinate governance addendum (not a numbered ADR) |
| **Subordinate to** | ADR-011 — Historical Trading-Calendar Authority Window |
| **Related** | ADR-003 (broker adapter boundary), ADR-010 (runtime lifecycle & managed tasks), ADR-004 (NSE cash-equity domain) |
| **Status** | Accepted (principles); **implementation DEFERRED** — sequencing decision **B** |
| **Date** | 2026-08-16 |
| **Deciders** | Market-Engine / Platform Architecture |
| **Changes to ADR-011** | None. Dhan is declared **secondary monitoring evidence**, never an authority source. |

---

## Context

A public, unauthenticated Dhan page (`https://dhan.co/market-holiday/`) publishes NSE/BSE/MCX
holidays, weekend closures, Muhurat info, and equity market timings. It is proposed as a
daily monitor that detects drift between what Dhan publishes and ApexScan's authoritative
calendar. This addendum governs that monitor so it can never erode the ADR-011 authority model.

## Load-bearing decisions

- **MON1 — Authority classification.** Dhan's page is **SECONDARY MONITORING EVIDENCE / discovery
  only.** The sole authoritative trading calendar remains the governed, versioned,
  NSE-primary-evidence-backed dataset of ADR-011 (pending ADR-011-DATA-R1-R4). A scraped Dhan
  value **must never** add `closed_dates`/`open_sessions`, create a `TradingSessionOverride`,
  expand `CalendarCoverage`, alter reconstruction, change strategy readiness, or mark a
  historical requirement satisfied. A discrepancy is **evidence for human review**, not an
  authority mutation.
- **MON2 — Boundary (provider-neutrality preserved).** All HTTP + HTML parsing is provider-
  specific and lives behind the adapter boundary (`app/adapters/dhan/`), with the monitor
  orchestration in `app/services/`. **No** Dhan URL, HTML selector, HTTP client, scraping type,
  or provider model may appear in `market_engine/`, `strategies/`, or `strategy_manager/`
  (ADR-003).
- **MON3 — Source scope.** Fetch/parse only NSE **cash-equity** calendar facts (ADR-004). Do
  **not** fold MCX-only, clearing-only, or BSE-only dates into NSE closures.
- **MON4 — Ownership & scheduling.** One application-owned managed daily task, created and
  owned per ADR-010 D9 (owned handle, cancel+await on shutdown, done-callback). It runs once
  each trading-day morning **before** calendar classification is needed. The execution time is
  **governed Settings configuration**, never a hidden literal. No per-instrument/per-strategy/
  per-holiday tasks. It composes as a `ProviderDependency`-shaped seam or a sibling managed
  task — decided at implementation, consistent with ADR-010.
- **MON5 — Comparison semantics.** Compare the normalized Dhan observation against the
  authoritative ADR-011 dataset for the relevant dates, yielding exactly one of: `MATCH`,
  `DHAN_NEW_CLOSED_DATE`, `DHAN_NEW_OPEN_DATE`, `DHAN_DATE_STATUS_CONFLICT`,
  `DHAN_SESSION_TIMING_CHANGE`, `DHAN_PARSE_FAILURE`, `DHAN_FETCH_FAILURE`,
  `AUTHORITATIVE_COVERAGE_MISSING`. The observation model mirrors the packaged-dataset model
  (`closed_dates` / `open_sessions` / `TradingSessionOverride`) so comparison is apples-to-apples.
- **MON6 — Fail-closed.** Unreachable → no calendar change, surface status. HTML structure
  changed or parse ambiguous → **do not guess**, surface `DHAN_PARSE_FAILURE`. Dhan contradicts
  NSE authority → **retain NSE**, surface `DHAN_DATE_STATUS_CONFLICT`. No authoritative dataset
  loaded → `AUTHORITATIVE_COVERAGE_MISSING` (never treat Dhan as the fallback authority).
- **MON7 — Special-session safety (H3).** A page stating "Muhurat" proves only what it states.
  Never invent session intervals; never collapse multi-interval sessions into an envelope. An
  exceptional OPEN date without complete interval info may raise a **date-level** discrepancy
  while intraday timing remains unavailable/fail-closed (ADR-011 multi-interval addendum, H3).
- **MON8 — Observability.** Emit structured status: `last_attempt_at`, `last_success_at`,
  `source`, `source_year`, `status`, `difference_count`, `parse_status`. **Never** log the full
  HTML document; no credentials exist to log.
- **MON9 — Bounded change-detection state.** Keep just enough state (a discrepancy signature) to
  avoid re-alerting the same discrepancy every morning; **no** unbounded HTML/history archive.
  If durable audit history is wanted, the existing DB architecture owns it — decided at
  implementation, not invented here.
- **MON10 — Frozen invariants.** The monitor never changes `staged_observation_verified=False`,
  `tick_aggregate_verified=False`, or `supports_current_day=False`; never activates historical
  warmup because a scrape succeeded; never implements a strategy.
- **MON11 — Network policy / determinism.** No network in deterministic domain tests (captured
  synthetic-HTML fixtures for the parser; a fake HTTP transport for integration). CI must not
  depend on `dhan.co` availability. No `date.today()`/`now()` inside domain logic — the morning
  run receives an explicit reference instant.

## Sequencing decision — **B (govern now, implement after ADR-011-DATA-R1-R4)**

The monitor's core function is **comparison against the authoritative dataset**, which **does
not yet exist** (ADR-011-DATA-R1-R4 is BLOCKED on the enumerated NSE holiday list; ADR-011-IMPL
has not wired a queryable authoritative calendar). Implementing the comparator now would:
(1) degenerate every result to `AUTHORITATIVE_COVERAGE_MISSING` (no baseline), and (2) risk
building the normalized observation model before the packaged-dataset model is finalized. Per
"no speculative features / build when prerequisites are proven", the monitor is **governed now
and implemented after** the authoritative dataset exists and is queryable. This monitor is
**not** a substitute for ADR-011-DATA-R1-R4 (§15). It was **not rejected** (C) — an existing
mechanism search found none and the architecture fits cleanly as a secondary seam.

## Future implementation contract (deferred)
Under `app/adapters/dhan/` a pure HTML→normalized-observation parser (fixture-tested); under
`app/services/` a lifecycle-owned daily monitor (ADR-010 D9) that fetches via an injected HTTP
transport, normalizes NSE-equity facts (MON3), compares against the loaded authoritative
calendar (MON5), surfaces discrepancies + structured status (MON6/MON8), and keeps bounded
change-detection state (MON9). A governed Settings field for the run time; a fake-transport
integration test; the full test matrix below.

## Future test matrix
Parses NSE weekday holiday; ignores MCX-only; ignores clearing-only; recognizes weekend holiday;
recognizes Muhurat/exceptional-OPEN marker; missing timing does not fabricate timing; `MATCH`;
new-closed discrepancy; new-open discrepancy; conflicting status; malformed HTML → fail-closed;
HTTP failure preserves authoritative state; duplicate observation → no unbounded alerts; **zero
authority mutation**; Market Engine remains Dhan-free; authority bits remain False;
`supports_current_day` remains False.

## Consequences
**Positive.** The boundary that keeps Dhan strictly secondary is fixed durably before any code
exists, so the monitor can never silently become the calendar authority. **Neutral.** No code
this phase. **Deferred.** Implementation waits for the authoritative dataset so the comparator
has a real baseline.
