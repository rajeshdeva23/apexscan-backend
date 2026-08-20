# ADR-007 Addendum — Partial-Universe / Per-Instrument Historical Readiness

| Field | Value |
|-------|-------|
| **Type** | Subordinate governance addendum (not a numbered ADR) |
| **Subordinate to** | ADR-007 — Dynamic Strategy Lifecycle & Requirement Management (refines the D3 START readiness boundary; does not rewrite any Accepted decision) |
| **Related** | ADR-006 (candle completeness), ADR-010 (task lifecycle), ADR-011 (calendar authority / dataset-failure policy), ADR-012 (scanner + REST addendum), ADR-013 (strategy registration) |
| **Status** | Accepted (decision + implementation contract); **implementation DEFERRED** to NARROW-CPR-PARTIAL-UNIVERSE-READINESS-IMPL-R1 |
| **Date** | 2026-08-16 |
| **Deciders** | Strategy / Market-Engine Architecture |
| **Decision** | **Option B — separate strategy-lifecycle readiness (infrastructure) from per-instrument evaluation readiness.** A scanner strategy reaches `RUNNING` when its warmup mechanism executes without a *global* failure; each instrument is then gated per-`MarketContext` at evaluation time (already implemented). One un-warmable instrument no longer forces the whole strategy to `ERROR`; the scanner reports `PARTIAL` honestly. |

---

## Context & the discovered limitation

`NARROW-CPR-E2E-VALIDATION-R1` proved the vertical slice and surfaced an all-or-nothing
robustness limit: if any single instrument cannot satisfy `HistoricalRequirement(session, 1)`,
`RequirementsCoordinator.warm` returns False and `StrategyManager.start` marks the whole strategy
`ERROR` → zero results → `scanner_snapshot("narrow_cpr")` is `None`. For a ~208-instrument F&O
universe (a newly-listed symbol, a per-symbol provider gap, a missing previous session), one bad
instrument suppresses the entire scan.

## Current behaviour — proven from code (not inferred)

- `StrategyManager.start` (`app/strategy_manager/manager.py`): `ready = await coordinator.warm(...)`;
  a raised exception → `mark_error` → `ERROR`; `if not ready: mark_error → ERROR`; else `mark_running`.
- `RequirementsCoordinator.warm` (`app/strategy_manager/requirements_bridge.py`): warms the effective
  union, then returns **`all(required <= satisfied.get(instrument, frozenset()) for instrument in
  self._instruments)`** — a universe-global AND across every instrument. **This is the sole
  all-or-nothing gate.**
- `assess_readiness` (`app/strategy_manager/readiness.py`) is **already per-instrument**: it evaluates
  a strategy's requirements against **one** `MarketContext`; `_historical_ready` checks *that context's*
  `historical.series`. An instrument whose context lacks the required series → `MISSING_HISTORICAL` →
  the manager skips it (not-ready), never an error.
- The warmup/planner failure taxonomy is **already split**: calendar/date failures
  (`OutsideCalendarCoverageError`, `MissingSessionTimingError`, `HistoricalWarmupUnavailableError`) and
  `AuthoritativeCalendarUnavailableError` (composition) are instrument-agnostic and **raise** (global);
  per-symbol `HistoricalSourceError`/`HistoricalDataQualityError` are **caught** → that instrument is
  unsatisfied. Only `warm`'s boolean over-collapses the per-instrument outcome.

**Conclusion:** per-instrument readiness and honest scanner `PARTIAL` already work *once the strategy
is RUNNING*. The only thing standing between the current state and partial-universe support is `warm`'s
`all(...)` START gate.

## ADR-007 compatibility

ADR-007 D3: "run dependency/readiness checks → trigger historical warmup if required → become RUNNING
**only after required dependencies are ready** (else ERROR)." "Required dependencies" names the
strategy's **dependency infrastructure** (config, calendar authority, historical subsystem), not "every
instrument in the universe has data." The `all(...)` collapse is an implementation choice, not an
ADR-007 mandate. D7 governs *evaluation* exceptions/threshold, not warmup coverage. So the §28 principle
— *strategy-lifecycle readiness = strategy + infrastructure operational; instrument readiness =
sufficient authoritative facts to evaluate that strategy for that instrument* — **fits** ADR-007. **No
STOP condition triggered.** This addendum refines the D3 readiness boundary; it does not rewrite ADR-007.

## Options assessed
- **A — keep all-or-nothing.** No Accepted ADR requires it; operationally fragile at 208 instruments. *Rejected.*
- **B — strategy RUNNING (infrastructure) + per-instrument readiness. *Selected.*** Reuses the existing
  per-context `assess_readiness` and the existing honest scanner `PARTIAL`; the only change is `warm`'s
  START-readiness meaning. Global failures still fail START closed.
- **C — minimum coverage threshold (e.g. ≥95%).** Adds ungoverned policy; not needed for a correct V1. *Deferred* (can layer on later if operations require a hard floor).
- **D — remove failed instruments from `expected_count`.** *Rejected* — hides missing instruments and
  falsely yields COMPLETE (violates ADR-012 NCRS10 honesty).
- **E — publish per-instrument error results.** *Rejected for V1* — would change the public pipeline
  (SKIPPED/ERROR are internal, never published); scanner `PARTIAL` + counts already communicate the truth.

## Decisions PUR1–PUR23

- **PUR1 — Readiness ownership (two levels, both already present).** (i) *Strategy-lifecycle readiness*
  (START, `RequirementsCoordinator.warm`) = the strategy's dependency infrastructure is operational.
  (ii) *Per-instrument evaluation readiness* (`assess_readiness` per `MarketContext`) = that instrument
  carries the required authoritative facts. Instrument readiness is **already** per-instrument.
- **PUR2 — Lifecycle vs instrument readiness.** These are distinct concepts (ADR-007 D3 = level i).
  START must not require every instrument's data; evaluation gates each instrument (level ii).
- **PUR3 — Partial warmup semantics.** `warm` reports START readiness = "the warmup mechanism
  **executed** (returned) without a *global* failure." It no longer requires every instrument satisfied.
  Warmup still installs an authoritative `HistoricalContext` per **satisfied** instrument; unsatisfied
  instruments simply get none.
- **PUR4 — Global failure semantics (→ START ERROR / composition fail-fast; fail-closed).** Invalid
  configuration; authoritative calendar dataset unavailable (`AuthoritativeCalendarUnavailableError`,
  at composition — ADR-011); no calendar coverage configured (`HistoricalWarmupUnavailableError`);
  requirement reaching before coverage (`OutsideCalendarCoverageError`); date-driven
  `MissingSessionTimingError` raised by the planner. These **raise** out of `start`/`warm` and keep the
  strategy out of `RUNNING`.
- **PUR5 — Instrument (local) failure semantics (→ instrument not-ready; strategy stays RUNNING).**
  Per-symbol `HistoricalSourceError`, `HistoricalDataQualityError`, a missing previous-session candle,
  or insufficient candles for that symbol → caught during warmup → the instrument is unsatisfied → no
  `HistoricalContext` installed → evaluation-time `MISSING_HISTORICAL` → skipped → absent from the
  scanner.
- **PUR6 — Zero-ready semantics.** `0/N` ready → the strategy remains `RUNNING` with zero eligible
  candidates; the scanner honestly reports `PARTIAL`, `eligible_count = 0`. **Not** `ERROR` (the
  mechanism operated; nothing is fabricated). A coverage-floor policy (Option C) is deferred.
- **PUR7 — Scanner `expected_count`.** Remains the canonical runtime universe size; **never** reduced
  because instruments failed warmup (Option D rejected).
- **PUR8 — Scanner completeness.** **Unchanged** (ADR-012 NCRS10): COMPLETE iff
  `evaluated_count == expected_count`, else `PARTIAL`. The scanner already reports this honestly once
  the strategy is RUNNING — **no scanner change**.
- **PUR9 — No-fabrication invariant.** A missing instrument produces **no** result and **no** candidate.
  Never `cpr_width_pct = 0`, a rank, a `NO_MATCH` due to missing data, a synthetic candidate, `calendar−1`,
  current-day/partial/stale/nearest session, or settings/monitor/synthetic/zero OHLC. Absence stays
  distinguishable from a genuine `NO_MATCH`.
- **PUR10 — Requirement-union semantics.** **Unchanged** — the effective union is still composed across
  strategies; `warm` still warms it; readiness is judged per strategy's own requirements per context.
  No Narrow-CPR special-casing in the coordinator.
- **PUR11 — Retry policy.** **None added.** A failed instrument stays unavailable for the session; it may
  recover on the next authoritative warmup / restart. Any future retry stays bounded and ADR-010-compliant
  (no per-instrument task).
- **PUR12 — Concurrency / task ownership.** **Unchanged** (ADR-010): no per-strategy/per-instrument task
  or scheduler; `HistoricalCoordinator` remains the bounded concurrency owner; the scanner stays task-free;
  `asyncio.create_task` sites stay at **3**.
- **PUR13 — Observability.** Bounded via the existing `ScannerSnapshot` counts
  (`expected_count`/`evaluated_count`/`eligible_count`/`completeness`); no per-symbol unbounded log/history,
  no DB. A bounded ready/not-ready count MAY be logged at warmup; not required.
- **PUR14 — API impact.** **None** — the REST response already carries the four counts + `completeness`;
  `PARTIAL` is already representable (ADR-012 REST addendum). No new field.
- **PUR15 — Provider neutrality.** The change lives in the broker-neutral coordinator/readiness layer; no
  provider type; no `if strategy_id == …`.
- **PUR16 — Calendar authority.** ADR-011 unchanged; calendar/date failures stay fail-closed and **global**
  (instrument-agnostic) — never collapsed with per-instrument data gaps (STOP-7 addressed).
- **PUR17 — Current-day / session-statistics isolation.** `supports_current_day=False`,
  `staged_observation_verified=False`, `tick_aggregate_verified=False` unchanged; partial readiness grants
  no authority.
- **PUR18 — Zero-demand behavior.** **Unchanged** — a disabled strategy declares no requirement → zero
  historical demand; partial readiness never loads for disabled strategies.
- **PUR19 — Plug-and-play.** The readiness change is strategy-agnostic; Open=High/Open=Low/Momentum reuse
  it with no code change and no strategy-id branch.
- **PUR20 — Determinism.** Given the same warmup outcomes (same satisfied instruments), the same instruments
  are ready and the ranking/snapshot/REST body are identical.
- **PUR21 — Failure matrix.** Below.
- **PUR22 — Implementation boundary.** The **only** production change is `RequirementsCoordinator.warm`:
  return START-readiness = "warmup executed" (global errors still propagate → START ERROR) instead of
  `all(per-instrument satisfied)`. **No** change to `readiness.py` (already per-context), the scanner, the
  API, the Narrow CPR calculator/evaluate, calendar authority, provider adapters, runtime task ownership,
  or the `Strategy` protocol. Plus tests + `warm`'s docstring.
- **PUR23 — Test matrix.** Below.

## Failure matrix (PUR21)

| # | Scenario | Strategy lifecycle | Instrument readiness | Scanner | `expected_count` | `evaluated_count` | Retry | Logged |
|---|----------|--------------------|----------------------|---------|------------------|-------------------|-------|--------|
| 1 | One instrument missing history | RUNNING | that one NOT_READY (skipped) | PARTIAL | full N | N−1 | no | count |
| 2 | Several missing history | RUNNING | those NOT_READY | PARTIAL | full N | N−k | no | count |
| 3 | All missing history | RUNNING | all NOT_READY | PARTIAL, `eligible=0` | full N | 0 | no | count |
| 4 | One malformed candle (`HistoricalDataQualityError`) | RUNNING | that one NOT_READY | PARTIAL | full N | N−1 | no | count |
| 5 | One outside calendar coverage | **START ERROR** (global; date-driven, instrument-agnostic) | — | `None` snapshot | — | — | no | error |
| 6 | `MissingSessionTimingError` (date-driven) | START ERROR (global) | — | `None` | — | — | no | error |
| 7 | Provider call fails for one symbol (`HistoricalSourceError`) | RUNNING | that one NOT_READY | PARTIAL | full N | N−1 | no | count |
| 8 | Provider unavailable for all symbols (all source-fail, none raise global) | RUNNING | all NOT_READY | PARTIAL, `eligible=0` | full N | 0 | no | count |
| 9 | Authoritative dataset unavailable | **composition fail-fast** (ADR-011) — runtime never composes | — | n/a | — | — | no | error |
| 10 | Strategy configuration invalid | **START ERROR** (global) | — | `None` | — | — | no | error |

## Future test matrix (PUR23)
A 208/208 → RUNNING+COMPLETE; B 207/208 → RUNNING+PARTIAL; C 205/208 → RUNNING+PARTIAL; D newly-listed
instrument missing previous session → that one absent; E provider failure for one → absent; F incomplete
previous session → absent; G malformed candle → absent; H 0 ready → RUNNING+PARTIAL(0); I global historical
source unavailable → START ERROR; J calendar dataset unavailable → composition fail-fast; K missing session
timing → global; L outside coverage → global; M disabled → zero demand; N a second strategy unaffected by
Narrow CPR's missing instrument; O shared requirement union still deduplicated; P no fabricated
`StrategyResult`; Q no fabricated scanner candidate; R `expected_count` = canonical universe; S scanner
PARTIAL honest; T API returns PARTIAL honestly; U no new `create_task`; V no per-instrument scheduler; W
deterministic; X provider-neutral; Y authority flags False; Z full regressions.

## STOP-condition assessment
None triggered: (1) ADR-007 D3 does not require every instrument ready before RUN; (2) partial readiness
adds no data weakening (unsatisfied instruments get *no* context); (3) the scanner honestly represents
missing instruments (full `expected_count`, `PARTIAL`); (4) no per-instrument task; (5) lifecycle vs
instrument readiness separate without redesign (per-context readiness already exists); (6) requirement-union
semantics unchanged; (7) calendar failures classify cleanly as global (raise) vs local (caught); (8) no
other Accepted ADR pins this — it is a subordinate refinement of ADR-007 D3.

## Files changed
Docs-only: this addendum + the README index row. No `app/`/`tests/` change this phase.

## Consequences
**Positive.** One un-warmable instrument no longer suppresses the whole scan; the scanner reports honest
`PARTIAL` over the full canonical universe; authoritative history stays strict (no fabrication); the change
is a single, generic, provider-neutral edit to `warm` with no scanner/API/calculator/task change.
**Negative / accepted.** A widespread outage that never raises a global error yields `RUNNING` + `PARTIAL(0)`
rather than `ERROR` (honest but requires operators to read the coverage counts); a coverage-floor is deferred.
**Neutral.** No code this phase.

## Exact implementation slice
**NARROW-CPR-PARTIAL-UNIVERSE-READINESS-IMPL-R1** — change `RequirementsCoordinator.warm` START-readiness to
"warmup executed" (global failures still propagate → START `ERROR`), keeping per-context `assess_readiness`
and scanner `PARTIAL` as-is; add the PUR23 test matrix; all gates; invariants preserved. Generic despite the
phase name (no Narrow-CPR branch).
