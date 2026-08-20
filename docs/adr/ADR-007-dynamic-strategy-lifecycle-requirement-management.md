# ADR-007 — Dynamic Strategy Lifecycle & Requirement Management

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-09 |
| **Deciders** | Platform / Strategy Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Refines** | `docs/07_STRATEGY_ENGINE.md` (§5 lifecycle, §6 manager, §7 contract, §12 results, §13–§14 scoring/ranking, §16 dependencies) |
| **Related** | `docs/06_MARKET_ENGINE.md` (§13 candles, §18 features, §19 events), `docs/02_DATABASE_DESIGN.md` (Strategies / Strategy Config / Strategy Runs / Strategy Results), `docs/08_API_SPECIFICATION.md` (§4 security), `docs/09_WEBSOCKET_FLOW.md`, ADR-003, ADR-005, ADR-006 |

---

## Context

`docs/07_STRATEGY_ENGINE.md` is Official and normatively fixes the Strategy
Engine framework: the conceptual contract (§7), the Open-Closed plugin model
(rules 29/30), the Strategy Manager (§6), registration (§9), the immutable,
versioned `StrategyResult` (§12), strategy-owned scoring / engine-owned ranking
(§13–§14), fault isolation and determinism (§11, §20), and read-only MarketContext
consumption (§4.11). `docs/02` governs persistence (Strategies, Strategy Config,
Strategy Runs, Strategy Results). The Phase-5 design/preflight validated the
framework against the current code and confirmed it is implementable without
redesign.

The preflight also found five **operational semantics that docs/07 leaves silent**
and that must be decided *once, at framework level* — not encoded ad hoc inside
individual strategies:

1. **Lifecycle verbs.** docs/07 §5 defines only non-destructive *enable/disable*
   plus auto-disable/shutdown. It does not define `START / PAUSE / RESUME / STOP /
   FORCE STOP` or the transitions between them.
2. **Requirement lifecycle.** docs/07 §16 says strategies *declare* dependencies
   and never fetch, but does not say what happens to a strategy's historical and
   live requirements as it pauses, stops, or errors.
3. **Dynamic live-timeframe registration.** P4.4's `CandleEngine` fixes its
   timeframe set at construction (no add/remove method). Strategies that require a
   live candle timeframe need a way to change the engine's active timeframe set at
   runtime without per-strategy candle engines and without the engine learning
   strategy identities.
4. **Result emission / dedup.** docs/07 publishes a ranked result set per context
   cycle; for a 208-instrument scanner, republishing identical matches on every
   tick is untenable. A dedup/transition policy is undefined.
5. **Emission modes** (one-shot vs edge-triggered vs continuous) are undefined.

These decisions cross two layers (Strategy Engine + an additive Market-Engine
seam) and one delivery concern (emission), so they are recorded as an ADR rather
than a single-document addendum.

## Decision Drivers

- Adding strategy #N must require no change to the engine, the manager core, the
  Market Engine, or the Data Provider (docs/07 rule 30; ADR-003 Open-Closed).
- Strategies remain read-only fact consumers; they never fetch, mutate, or know
  about each other (docs/07 §4.11).
- Determinism and per-instrument ordering are preserved (docs/07 §11; docs/06 §19).
- The Market Engine never learns strategy identities (docs/06/07 boundary).
- No fabrication of unobserved data (ADR-005/006 carry into live-timeframe changes).
- Persisted *intent* survives restart; runtime state is ephemeral (docs/03 §26,
  docs/02 storage matrix).

## Decision

### D1 — Runtime lifecycle states

The Strategy Manager owns a per-strategy runtime state machine, distinct from the
durable enabled/disabled *intent* (D9):

`REGISTERED · STARTING · RUNNING · PAUSED · STOPPED · ERROR · SHUTDOWN`

### D2 — Legal transitions (all others fail closed)

| From | To | Trigger |
|------|----|---------|
| REGISTERED | STARTING | start |
| STARTING | RUNNING | dependencies ready |
| STARTING | ERROR | start/validation/readiness failure |
| RUNNING | PAUSED | pause |
| PAUSED | RUNNING | resume |
| RUNNING | STOPPED | stop |
| PAUSED | STOPPED | stop |
| RUNNING | ERROR | repeated evaluation failure/timeout past threshold |
| STOPPED | STARTING | explicit start (restart) |
| ERROR | STARTING | explicit restart |
| ERROR | STOPPED | explicit stop |
| RUNNING · PAUSED · ERROR · STARTING (clean-cancellable) | STOPPED | **force stop** |
| any runtime state | SHUTDOWN | engine shutdown |

An illegal transition is rejected (fail closed); it never partially applies.
Restarting a `STOPPED` strategy re-enters `STARTING` and re-runs the full START
semantics (D3) — config validation, requirement re-registration, readiness/warmup
— never a direct jump to `RUNNING` and never via a redundant `REGISTERED` hop.
"STARTING (clean-cancellable)" above is an *orchestration precondition* the manager
proves before a force stop, **not** a separate FSM state: the FSM edge is simply
`STARTING → STOPPED`.

### D3 — START

Validate typed configuration → register the strategy's historical requirements
(D5) and live-timeframe requirements (D6) under its opaque `strategy_id` →
run dependency/readiness checks → trigger historical warmup if required → become
`RUNNING` **only after** required dependencies are ready (else `ERROR`). START
never restarts the Market Engine, never reconnects the provider unnecessarily,
and never disturbs another strategy's requirements.

### D4 — PAUSE / RESUME

**PAUSE** stops evaluation and result production but **retains** the strategy's
registered historical + live requirements and its per-session state, enabling
instant **RESUME** (`PAUSED → RUNNING`) with no re-registration and no warmup.
PAUSE never shrinks shared requirements. If a retained requirement became
unavailable meanwhile (external failure), readiness is re-checked before
evaluation resumes.

### D5 — STOP

Stop evaluation and result production → **deregister** this strategy's historical
and live requirements → release only those requirements no longer needed by any
other consumer (shared ones survive) → retain the static registration so the
strategy can be started again. STOP never disconnects the provider, never stops
the Market Engine, and never stops or deregisters another strategy.

### D6 — FORCE STOP

A manager-level lifecycle transition (never OS/thread/process kill), valid from
`RUNNING / PAUSED / ERROR / STARTING` (when cleanly cancellable): evaluation stops
immediately, requirement registrations are removed (D5 release rules apply), and
any strategy-owned pending work is cancelled. Other strategies, the Market Engine,
and the Data Provider are unaffected.

### D7 — ERROR / auto-disable

A single evaluation exception/timeout yields a safe `ERROR` evaluation (typed
reason code, no secrets/traces), increments an error counter, and isolates the
strategy (the pipeline and other strategies are unaffected — docs/07 §20).
Repeated failures past a threshold transition the strategy to `ERROR` and stop
evaluation. **ERROR retains requirements** until an explicit STOP, so the strategy
can be diagnosed and restarted without disturbing shared facts.

### D8 — Requirement ownership and the dynamic Market-Engine seam

- **Historical requirements** reuse the existing `HistoricalRequirementRegistry`
  (P4.5A) with `strategy_id` as the opaque, registry-local consumer key.
- **Live-timeframe requirements** use a new, symmetric, broker-neutral
  `LiveTimeframeRequirementRegistry`: `consumer_key → frozenset[Timeframe]`, with
  the effective set being the **union**. The Strategy Manager owns both registries.
- The **Market Engine gains one additive seam** to receive the recomputed
  effective union — conceptually `set_required_timeframes(frozenset[Timeframe])`
  (or an equivalent register/deregister façade that internally computes the union).
  The `CandleEngine` receives **only the effective timeframe set** and never per-
  strategy bookkeeping or strategy identities. This is an *additive* extension of
  the Phase-4 contract (consistent with P4.4's generic-timeframe design), not a
  redesign; it is implemented in the P5.4 slice.

### D9 — Dynamic timeframe add/remove semantics

- **Add mid-session:** future accepted ticks begin building the new timeframe;
  completed buckets *before* registration are **not** fabricated from the live
  stream (ADR-005/006). Any authoritative past candles come only through the
  Historical Context; live aggregation begins cleanly at the first observable
  bucket. If exact same-session backfill is not proven available, the new live
  timeframe has no authoritative past candles.
- **Remove (final consumer gone):** the engine may drop that timeframe's bounded
  active state; no other timeframe is affected; already-published `MarketContext`
  versions remain immutable; `HistoricalContext` is governed independently. A later
  re-add follows normal add semantics.

### D10 — Evaluation vs Result; emission modes; dedup

- **`StrategyEvaluation`** is the internal outcome of *every* evaluation.
  **`StrategyResult`** is the immutable, externally published/persisted fact,
  emitted only when the emission policy says it is material. Unchanged repeated
  NO_MATCH/MATCH evaluations are not published every tick.
- **Emission modes** (framework-level, declared per strategy in its spec, applied
  by the manager/dedup layer): `CONTINUOUS` (publish on material content change;
  suppress unchanged repeats), `EDGE_TRIGGERED` (publish transitions, e.g.
  `NO_MATCH → MATCH`, optionally `MATCH → NO_MATCH`), `ONE_SHOT_PER_SESSION`
  (first qualifying match per instrument/session publishes; later suppressed).
- **Dedup identity:** `(strategy_id, instrument, trading_date, emission-policy
  semantic key)`. `context_version` versions the observation but does **not** by
  itself force publication of an identical unchanged match. **Material change** is
  defined over typed fields — `status`, `score`, `confidence`, `reason_codes`,
  `metrics` — never over formatted English reason text.

### D11 — Score / rank relationship

Strategy owns the **score** (model); the Strategy Manager owns the **rank**
(presentation ordering), which never re-computes or overrides a score (docs/07
§13–§14, rules 8/19). Ranking is **not** part of `StrategyResult` semantic
equality (a result must not change because another instrument's score moved):
the immutable `StrategyResult` is the fact, and a separate `RankedStrategyResult`
projection carries ordering. Dedup is evaluated on the immutable result, before/
around ranking, consistently.

### D12 — Configuration changes (V1)

Configuration may change only when a strategy is `STOPPED` (or another explicitly
safe non-running state). Flow: STOP → validate new typed config → recompute
requirements → START. No arbitrary live mutable config swap in V1.

### D13 — Persisted intent vs runtime state; restart

Durable (PostgreSQL, via repositories): the enabled/disabled *intent* and typed
configuration (docs/02). Ephemeral: `STARTING/RUNNING/PAUSED/ERROR`. On restart,
persistently-enabled strategies are registered and started through the normal
dependency/warmup path; transient runtime objects are not persisted as process
state.

### D14 — Frontend / API / security boundary

Future UI controls (Start/Pause/Resume/Stop/Force Stop) map **only** to Strategy
Manager commands through the service/API boundary. The frontend never directly
touches the `CandleEngine`, the requirement registries, the provider, or the
`MarketContext`. Lifecycle commands are privileged Administration/Configuration
mutations under the docs/08 §4 RBAC model (least privilege, enforced at the
interface). V1 plugin implementations are application-registered Python code — no
user-supplied class/path/code, no dynamic filesystem/marketplace loading
(ADR-003 does not authorize it).

### D15 — Strategy specifications are separate

This ADR governs the *framework*. The exact rule, requirements, trigger,
configuration, emission mode, scoring model, and session-reset behavior of each
strategy (including Open=High, Open=Low, Narrow CPR) are defined in **individual
strategy specifications** authored separately, and are out of scope here.

## Normative example (shared requirements)

Strategy A requires `5m×100`; Strategy B requires `5m×20`, `15m×50`.

| State | Historical effective | Live effective |
|-------|----------------------|----------------|
| A + B RUNNING | `5m×100`, `15m×50` | `{5m, 15m}` |
| STOP A | `5m×20`, `15m×50` | `{5m, 15m}` |
| STOP B (then) | ∅ | ∅ |
| PAUSE A (instead of STOP) | unchanged | unchanged |

## Normative example (restart of a stopped strategy)

`RUNNING → STOP → STOPPED → START → STARTING → (requirements re-registered,
readiness/warmup ready) → RUNNING`. The strategy stays in the `StrategyRegistry`
throughout — STOP stops *runtime evaluation*, it never unregisters the strategy —
so restart is a lifecycle transition, not a re-registration of the implementation.
STOP deregistered its requirements; the restart's START re-registers them (D3/D5).

## Consequences

**Positive:** one framework-level answer to lifecycle, requirements, and emission;
adding a strategy stays plug-in-only; the Market Engine stays strategy-blind; no
fabrication; deterministic; restart-safe; frontend/security boundaries explicit.

**Negative / accepted:** one additive Market-Engine seam (`set_required_timeframes`)
must be built in P5.4 (additive, not a Phase-4 redesign); a new
`LiveTimeframeRequirementRegistry` mirrors the historical one; the dedup layer adds
manager-side state keyed per `(strategy, instrument, session)`.

## Alternatives considered

- **Construct `CandleEngine` with a static superset of all strategies' timeframes.**
  Rejected: strategies start/stop at runtime; a static superset either over-fetches
  forever or requires engine reconstruction (a redesign).
- **Per-strategy candle engines.** Rejected: duplicates aggregation, breaks the
  single canonical stream, and multiplies cost across 208 instruments.
- **Publish every evaluation and dedup in the frontend.** Rejected: floods the bus/
  WebSocket and pushes framework policy into the UI (docs/04/07 forbid UI logic).
- **A docs/07 addendum instead of an ADR.** Rejected: the decision includes an
  additive Market-Engine contract change and cross-cutting emission semantics that
  exceed the Strategy Engine document's scope.

## Governance acceptance (answers now explicit)

1. START — D3. 2. PAUSE — D4. 3. RESUME — D4. 4. STOP — D5. 5. FORCE STOP — D6.
6. Retain requirements: `PAUSED`, `ERROR`. 7. Release requirements: `STOP`,
`FORCE STOP` (unused-only). 8. Consumer registrations: **Strategy Manager**.
9. Does `CandleEngine` know strategy IDs: **No** (D8). 10. Live timeframes changed
via the additive union seam (D8). 11. Timeframe added mid-session: builds forward,
no live backfill fabrication (D9). 12. Final consumer stops: bounded state dropped,
others unaffected (D9). 13. Repeated identical matches suppressed by emission
policy + typed material-change dedup (D10). 14. Emission modes: CONTINUOUS /
EDGE_TRIGGERED / ONE_SHOT_PER_SESSION (D10). 15. Score owner: **strategy** (D11).
16. Rank owner: **Strategy Manager** (D11). 17. After evaluation error: safe ERROR
+ isolation, threshold → ERROR state, requirements retained (D7). 18. Config
changes: STOP → validate → recompute → START (D12). 19. Survives restart: enabled
intent + config (D13). 20. Frontend can stop one strategy without affecting others:
yes, via manager FORCE STOP/STOP isolation (D6/D14).
