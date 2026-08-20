# ADR-010 — Live-Market Runtime Composition & Managed Ingestion Lifecycle

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-12 |
| **Deciders** | Platform / Market-Engine Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Complements** | ADR-003 (broker adapter), ADR-004 (NSE cash-equity V1 universe), ADR-007 (strategy lifecycle & requirements), ADR-008/009 (session statistics) |
| **Refines** | `docs/03_BACKEND_ARCHITECTURE.md` (§6 boot sequence, §23–24 integration seams, §26 observability), `docs/06_MARKET_ENGINE.md` (§24 engine-as-hub) |
| **Related** | ADR-005 (session cumulative volume), ADR-006 (candle completeness / feed continuity), `ADR-009-refresh-phase-execution-addendum.md` |

---

## Context

The application has a complete, provider-neutral set of runtime components — a broker
adapter (ADR-003), a canonical instrument universe path (ADR-004), the Market Engine
(`InstrumentStateRegistry`, `TickEngine`, `CandleEngine`, session classifier), the
Strategy Manager runtime with its requirement bridges (ADR-007), historical warmup, and
the REST session-statistics refresh services (ADR-008/009). Every component is
independently implemented and unit/contract tested.

However, the production composition root (`app/main.py`) currently wires only PostgreSQL
and Redis. The P4.6E7 preflight and the P4.6E7A design/preflight found there is **no
governed owner** for the assembled live-market runtime: no owner for the shared
`InstrumentStateRegistry`, the shared `EventBus`, the `TickEngine`, the Strategy Manager
runtime, the requirement bridges, the runtime instrument universe, the managed live
ingestion task, startup/shutdown ordering, runtime failure isolation, or the
provider-credential-absence policy. The architecture docs describe a *composition root*,
a *composition bridge outside the engine*, and a *services-command* pattern, but name no
runtime-owner object. Introducing one is a new architectural seam that must be governed
before production wiring (RUN-A → RUN-E) may begin.

This ADR decides that ownership. It does **not** change any existing phase decision: the
Market Engine remains provider-blind and the sole `MarketContext` writer; strategies
remain provider-blind; one accepted datum still yields at most one `MarketContext`
version and no statistics-only event; ADR-007 requirement lifecycle, ADR-009 REST staging,
and E6A per-source authority separation all remain in force. Session-statistics authority
remains disabled and current-day historical reconciliation remains disabled.

## Decision

### D1 — Runtime owner
A new broker-neutral **`LiveMarketRuntime`** is the production owner of the assembled live
market pipeline. It is application/composition-layer infrastructure (recommended location
`app/services/market_runtime.py`; this ADR governs the role, not the filename). It is
**not** the Market Engine, **not** provider-specific, and **not** the Strategy Manager. It
owns runtime object assembly and long-running task lifecycle, and coordinates existing
components without absorbing their business logic. It **may** implement the existing
`ProviderDependency` lifecycle contract (`start(timeout_seconds)`, `verify_health()`,
`shutdown()` — `app/core/lifecycle.py`) — **this reuse is accepted** as the narrowest fit.
No second `ApplicationLifecycle` is introduced.

### D2 — Application lifecycle ownership
`ApplicationLifecycle` remains the top-level owner. It receives the composed
`LiveMarketRuntime` through its **existing optional `provider: ProviderDependency` seam**
rather than constructing Market Engine internals itself. `ApplicationLifecycle` retains
boot sequencing, readiness integration, and shutdown coordination for DB/Redis and the
runtime dependency; `LiveMarketRuntime` owns live-subsystem startup, managed ingestion,
and subsystem shutdown. DB/Redis ownership does **not** move into `LiveMarketRuntime`.

### D3 — Single shared runtime objects (load-bearing)
Exactly one instance of each of the following is constructed by `LiveMarketRuntime` and
shared as stated; duplicate isolated instances for convenience are forbidden:
- **`InstrumentStateRegistry`** — shared by `TickEngine`, `HistoricalWarmupService`, and
  `SessionStatisticsRefreshService` (else staged observations/history never surface).
- **`EventBus`** — shared by the `TickEngine` publisher and the `StrategyManager` subscriber.
- **`CandleEngine`** — shared by `TickEngine` and the `CandleEngineTimeframeSink` used by
  the `RequirementsCoordinator`.
- **`RequirementsCoordinator`** — shared by Strategy Manager lifecycle commands and the
  historical/live/fact registries.
- **canonical instrument-universe value** (`tuple[Instrument]`) — shared by the registry's
  known instruments, the provider subscription request, the historical warmup /
  requirements coordinator, and the `SessionStatisticsRefreshCoordinator`.

### D4 — Provider composition
The concrete provider is constructed **only at the composition boundary**
(`DhanRestAdapter.from_settings(settings)` for Dhan V1), which may implement live market
data, historical data, instrument data, and the session-statistics source on one adapter
instance. Provider-specific construction stays outside `app.market_engine`,
`app.strategies`, and `app.strategy_manager` core. No provider-name switch enters the
engine. Provider selection is **V1 configured composition** (the composition root chooses
the adapter), not a hard-coded engine decision; a general provider-selection setting is a
later concern.

### D5 — Instrument universe
The production canonical universe is resolved from the governed instrument-data provider
path (Dhan V1: `load_instruments()` → `load_fno_stock_universe()` → `tuple[Instrument]`,
i.e. the ADR-004 cash-equity universe). The runtime does **not** hard-code the ~208
symbols. The universe is canonical, immutable for the running runtime instance, resolved
once at startup, and reused by all relevant components. **V1 refresh policy:** resolve once
per runtime start; rebuild on application restart. No intraday universe-refresh task is
introduced.

### D6 — Live subscription universe
The runtime subscribes the **whole governed cash-equity universe** independent of strategy
count (ADR-004: the 208 set is the bounded live-subscription domain). Strategy demand
controls **timeframes and facts**, not the instrument set. Zero strategies still means the
live provider runtime is up and `MarketContext`s flow, with no strategy evaluation, no
active candle timeframes, no historical warmup, and no session-statistics refresh.
Strategies do **not** own the market universe.

### D7 — TickEngine initial state
`TickEngine` is composed with the shared `InstrumentStateRegistry` and `EventBus`, the
canonical `MarketSessionClassifier` (schedule/calendar/timezone), the shared `CandleEngine`,
`SystemClock` in production, the existing monotonic sequence, and
`SessionStatisticsAuthority(staged_observation_verified=False, tick_aggregate_verified=False)`.
`CandleEngine` may start with **zero** required timeframes; **no fake 1m/5m default** is
introduced.

### D8 — Managed live ingestion
Exactly **one** managed live-ingestion task consumes the provider canonical stream and
dispatches **sequentially** into the `TickEngine`:
`async for datum in live_adapter.stream_market_data(subscription): tick_engine.process(datum)`.
No per-instrument fan-out, no uncontrolled concurrent `process()` calls, and no second
event-routing technology before the `TickEngine`. Rationale: preserve per-instrument
read-modify-write ordering and shared-sequence semantics, keep backpressure natural, and
prevent state races (the engine has no internal concurrency guard). The task is owned by
`LiveMarketRuntime`.

### D9 — Task ownership
Every long-running runtime task has an explicit owner, a stored task handle, idempotent
start, an explicit stop/shutdown path, cancellation with awaited completion, error
handling, no duplicate task after restart, and no orphan task after application shutdown.
Bare untracked `asyncio.create_task(...)` is forbidden. This governs the live-ingestion
task, the later session-statistics refresh driver, and any future runtime periodic task.
Provider-internal reconnect logic may remain inside the adapter (already governed by
ADR-006 feed-continuity).

### D10 — Startup order
Normative ordering (a step precedes the next because the later step depends on it):
1. PostgreSQL init + verify.
2. Redis init + verify.
3. Provider construction + `ProviderCoordinator.start(timeout)`.
4. Instrument-universe resolution.
5. Build shared runtime state/engine objects (registry, `EventBus`, `CandleEngine`,
   session classifier, `TickEngine`).
6. Build historical + session-statistics services.
7. Build strategy runtime + requirement bridges (`RequirementsCoordinator`).
8. `StrategyManager.subscribe()` to the shared `EventBus`.
9. Start the managed live-ingestion task.
10. Verify runtime health / allow application readiness.

**Invariant:** Strategy Manager subscribers are installed (step 8) **before** market
ingestion begins (step 9), so no initial `MarketContext` is lost. Steps 1–2 keep the
existing fail-fast data-store gate.

### D11 — Shutdown order
Normative ownership-level ordering:
1. Start no new runtime work.
2. Cancel the managed live-ingestion task and **await** it.
3. Stop future refresh-driver work (when present).
4. `StrategyManager.unsubscribe()`.
5. `ProviderCoordinator.shutdown()` (adapter disconnect).
6. Close Redis.
7. Dispose PostgreSQL.
Steps 5–7 reuse the existing `ApplicationLifecycle` reverse-order cleanup. No orphan tasks
survive shutdown.

### D12 — Failure domains
Distinct, never merged into one health boolean:
- **Provider auth/startup failure:** policy-dependent (see D14) — fail-fast or dormant.
- **Live disconnect:** provider reconnect policy (ADR-006); not an automatic crash.
- **Strategy evaluation failure:** isolated to the strategy (ADR-007 / P5.3); "skipped, not
  fatal".
- **Historical warmup failure:** a per-strategy dependency/readiness failure; not global
  corruption.
- **Session-statistics refresh failure:** fail closed — retain prior state, do not advance
  `as_of`, do not mark strategy evaluation ERROR, do not crash live ingestion.
- **DB/Redis startup failure:** existing fail-fast policy.

### D13 — Health / readiness
The existing model is preserved: liveness is independent of dependencies; application
readiness is gated on the pipeline being up (through startup step 10) and on provider/
runtime health; **strategy readiness does not gate application liveness/readiness** ("a bad
strategy is skipped, not fatal"). No new public endpoint is introduced here. When
`LiveMarketRuntime` implements `ProviderDependency`, `verify_health()` proves: the runtime
started, the provider/live-ingestion subsystem is healthy enough under the configured
policy, and no fatal task has terminated. It does **not** assert session-statistics
authority readiness.

### D14 — Provider-credential-absence policy
Environment is chosen by **explicit configuration**, never inferred from
hostname/process behavior.
- **Provider not configured (local / CI / dev):** the live-market runtime remains
  **dormant** — no real provider construction, no broker-credential requirement for ordinary
  unit/CI runs; the app still boots (health/version) under the existing environment policy,
  and provider-dependent readiness is reported per the existing readiness semantics.
- **Provider-enabled (production) mode:** missing/invalid required provider credentials
  **fail fast**; the application never silently falls back to synthetic market data in
  production.
This aligns with docs/03 §6 boot step 6 ("degrade or abort per policy") and ADR-003
("live-provider validation is opt-in and never a general CI dependency").

### D15 — Zero-strategy runtime
Production runtime may start with **zero** registered concrete strategies: the Strategy
Manager may subscribe but routes nothing; no requirements register; no historical warmup;
no session-statistics fact demand; no `StrategyResult` publication; `CandleEngine` may hold
zero active timeframes. No fake strategy is required for runtime health.

### D16 — Requirement bridges
`StrategyManager`/lifecycle → `RequirementsCoordinator`, which coordinates the
`HistoricalRequirementRegistry`, `LiveTimeframeRequirementRegistry`, and
`FactRequirementRegistry` through their bridges: historical → `HistoricalWarmupService`;
live timeframe → `CandleEngineTimeframeSink`; `SESSION_STATISTICS` fact →
`SessionStatisticsRefreshControl` (implemented by `SessionStatisticsRefreshCoordinator`).
No strategy directly touches these engines/services. (The composition wiring must pass the
`refresh_control`/`fact_requirements` seam, which the current factory omits — a RUN-D
composition task, not a redesign.)

### D17 — Current-day historical isolation
Runtime composition does **not** enable `supports_current_day=True`. `supports_current_day=False`,
`CURRENT_DAY_WITHHELD`, and `CURRENT_DAY_RECONCILIATION_GUARANTEE = NOT PROVEN` remain. REST
Market Quote session statistics are a live session-statistics source, not historical
reconciliation.

### D18 — Replay / determinism
The runtime performs I/O, but the deterministic core is unchanged: the same provider
canonical datums + recorded session-statistics observations + the same requirement/lifecycle
command sequence yield the same Market Engine / strategy outputs. Runtime wall-clock and I/O
scheduling must not enter deterministic domain contracts; `ManualClock`/fakes remain the
test seam.

### D19 — Bounded runtime
One runtime owner, one live-ingestion task, one shared state registry, one shared
`EventBus`, one canonical universe tuple; no per-strategy feed, no per-instrument ingestion
tasks; state bounded by the existing engine/services. No unbounded runtime history.

### D20 — Out of scope
This ADR does not decide: Open=High / Open=Low / first-5-minute rules; order execution;
option selection; frontend; strategy persistence; Redis strategy delivery;
session-statistics authority verification; or current-day historical enablement.

## Consequences

- Production wiring (RUN-A → RUN-E) can proceed against a governed owner without inventing
  ownership, a universe, or a second composition root.
- `ApplicationLifecycle` gains a runtime dependency through its existing seam — no contract
  change.
- Authority and current-day historical remain disabled; the E7 managed refresh driver and
  the later E6C authority enablement are unaffected by this ADR except that they now have a
  governed task-ownership home (D9) and phase policy (`ADR-009-refresh-phase-execution-addendum.md`).
- A provider-selection abstraction and an intraday universe-refresh policy are deferred; V1
  is single-provider configured composition with startup-resolved universe.

## Acceptance checklist
Owner = `LiveMarketRuntime` (D1); constructed at composition, started/stopped via
`ApplicationLifecycle` through `ProviderDependency` (D1/D2); one ingestion task, sequential
dispatch (D8); universe from the governed provider path, one shared value (D5); one shared
registry/`EventBus`/`CandleEngine` (D3); provider constructed only at the boundary (D4);
zero strategies (D15) and zero timeframes (D7) boot; credential absence governed (D14);
shutdown cancels+awaits the ingestion task (D11); provider/runtime health gates readiness,
strategy does not (D13); current-day historical disabled (D17); authority bits false (D7);
strategies cannot access the provider (D4/D16).
