# ADR-013 — Production Strategy Registration & Enablement (Strategy Catalog)

| Field | Value |
|-------|-------|
| **Status** | Accepted (architecture + contracts); **implementation DEFERRED** to NARROW-CPR-REGISTRATION-IMPL-R1 |
| **Date** | 2026-08-16 |
| **Deciders** | Strategy / Platform Architecture |
| **Complements** | ADR-007 (strategy lifecycle & requirement management, D8/D10/D15), ADR-010 (runtime composition & managed lifecycle, D5/D9/D13/D15), ADR-012 (cross-instrument scanner), ADR-004 (universe), ADR-011 (authoritative calendar/warmup) |
| **Related** | ADR-007 Narrow CPR strategy specification |

---

## Context

`NarrowCprStrategy` (ADR-007 Narrow CPR spec) is implemented and tested, and the generic
`CrossInstrumentStrategyScanner` (ADR-012) consumes `StrategyResultsPublished`. Repository
inspection confirms the **production wiring gap**:

- `LiveMarketRuntime.__init__` registers any passed `strategies` into `StrategyRegistry` +
  `StrategyLifecycle` (state `REGISTERED`) but **never starts them**; `start()` only subscribes
  the manager + scanner and creates the three managed tasks.
- `StrategyManager.start(strategy_id, *, reference)` is the **only** `REGISTERED → STARTING →
  RUNNING` transition (it registers the strategy's requirements, warms, then reaches RUNNING or
  ERROR). It is called **nowhere in `app/`** — only in tests.
- `compose_market_runtime` constructs `LiveMarketRuntime` with **no** `strategies`/`configurations`
  (both default empty) yet registers the scanner ranking policy — so a running production runtime
  can hold the `narrow_cpr` scanner policy while **no** `NarrowCprStrategy` is registered or
  running. The policy is inert.
- `Settings` has `strategy_error_threshold` but **no** strategy-enablement field.

So the production vertical flow is broken at one node: the `NarrowCprStrategy` is never
constructed/registered and never STARTED, so its `HistoricalRequirement(session, 1)` never enters
the requirement union → no warmup → no evaluation → no `StrategyResult` → the scanner never
receives a `narrow_cpr` result. This ADR governs the missing production **registration +
enablement** seam, plug-and-play for future strategies.

## Decisions REG1–REG16

- **REG1 — Registration owner: a provider-neutral strategy catalog.** A new broker-neutral
  **strategy catalog** (recommended `app/services/strategy_catalog.py`) is the canonical source of
  the production-known concrete strategies. The provider composition *invokes* it; it is **not**
  owned by the Dhan composition, and there is **no** import-time/filesystem auto-discovery
  (explicit + deterministic; docs/07 §4.2). (Options rejected: A1 Dhan module constructs strategies
  — couples the strategy list to a provider; A3 auto-discovery — non-deterministic import-time side
  effects; A4 — no existing owner, confirmed.)
- **REG2 — Catalog entry.** Each entry bundles the concrete `strategy` (a `Strategy` instance/
  factory), its default `StrategyConfiguration`, and an optional `ScannerRankingPolicy`. The entry
  validates fail-fast that `ranking_policy.strategy_id == strategy.descriptor.strategy_id` and that
  `configuration` is an instance of `strategy.configuration_type` — so a strategy, its config, and
  its scanner policy are declared and checked in **one place** (strategy-id drift eliminated by
  construction).
- **REG3 — Enablement is explicit.** A `Settings` field `strategies_enabled` (comma-separated
  strategy ids; **default empty**) selects which catalog entries are activated. Only listed,
  catalog-known ids are constructed, registered, configured, and started. An enabled id absent from
  the catalog fails composition fast (REG14). **Registered ≠ RUNNING.**
- **REG4 — Lifecycle stages (kept distinct).** *catalog* (known) → *enabled* (`strategies_enabled`)
  → *registered* (`StrategyRegistry` + `StrategyLifecycle`, state `REGISTERED`) → *configured* (in
  the manager's `configurations` map) → *STARTED/RUNNING* (`manager.start` at runtime start:
  `STARTING` → warm → `RUNNING`/`ERROR`) → *historical requirement active* (only after START, via
  `coordinator.register` + `warm`). No stage silently implies the next (ADR-007 D1/D2).
- **REG5 — Configuration ownership.** Production `NarrowCprConfiguration` comes from its **catalog
  entry's default** (`NarrowCprConfiguration(config_version="1.0.0")`, `narrow_cpr_max_width_pct=None`
  ⇒ rank-all). Strategy configuration lives in the catalog (the per-strategy configuration seam),
  **not** global `Settings` — no strategy-specific fields are added to `Settings`. Only the operational
  `strategies_enabled` list (and the existing `strategy_error_threshold`) are `Settings`.
- **REG6 — Start semantics.** `LiveMarketRuntime.start()` explicitly calls
  `await manager.start(strategy_id, reference=self._clock.now())` for each enabled strategy, **after**
  the manager/scanner subscribe and **before** (or alongside) ingestion begins. Per-strategy start
  failure is **isolated**: an `ERROR`/not-ready strategy is skipped and logged, never aborting the
  runtime or other strategies (ADR-010 D13 "a bad strategy is skipped, not fatal"). Non-enabled
  catalog entries are **never** auto-started (STOP #8 respected).
- **REG7 — Scanner-policy consistency.** The composition registers each entry's `ScannerRankingPolicy`
  into the scanner's `ScannerRankingPolicyRegistry` from the **same catalog entry** that supplies the
  strategy — replacing the current standalone `_SCANNER_RANKING_POLICIES` literal. `strategy_id` drift
  is eliminated by REG2; the metric-**name** ↔ strategy-output match stays covered by the existing
  composition drift-catch test. Scanner ranking semantics stay out of `evaluate()`; no fabricated score.
- **REG8 — Plug-and-play contract.** Adding a strategy (Open=High, Open=Low, Momentum, …) requires
  only: (1) the strategy implementation, (2) its `StrategyConfiguration`, (3) a catalog entry (+ a
  `ScannerRankingPolicy` if scanner-enabled), (4) adding its id to `strategies_enabled`. **No** change
  to the runtime, manager, or scanner, and **no** `if strategy_id == …` anywhere in generic code.
- **REG9 — Historical-demand lifecycle.** Enabled + started ⇒ `manager.start` → `coordinator.register`
  puts `HistoricalRequirement(session, 1)` into the effective union → `apply_live_union` → `await
  coordinator.warm(...)` warms via the dataset-backed authoritative historical path → `PREVIOUS_SESSION`
  ready → RUNNING → on the next `MarketContext` (`ON_HISTORICAL_READY`) evaluate → `StrategyResult` →
  `StrategyResultsPublished` → scanner → ranked snapshot. The scanner itself requests **zero** history.
- **REG10 — Zero-demand.** Default empty `strategies_enabled` ⇒ zero strategies registered/started ⇒
  zero Narrow-CPR historical demand ⇒ the scanner stays inert (`snapshot("narrow_cpr")` is `None`).
  This preserves today's safe zero-strategy production posture.
- **REG11 — Provider neutrality.** The catalog and strategies are broker-neutral (no Dhan type,
  `security_id`, `exchange_segment`, HTTP, WebSocket, or credentials). The provider composition may
  *invoke* the neutral catalog; strategy/catalog definitions stay broker-independent (ADR-003).
- **REG12 — Lifecycle / task ownership.** **No new task.** `manager.start` is awaited inline during
  `runtime.start()`; the manager routes via its single existing `EventBus` subscription (not per
  strategy). `create_task` sites remain **3** (ingestion / refresh / monitor). No per-strategy/
  per-instrument/per-candidate task (ADR-010 D9/D19).
- **REG13 — Current-day / authority isolation.** `supports_current_day=False`,
  `staged_observation_verified=False`, `tick_aggregate_verified=False` unchanged. Starting a strategy
  grants no current-day reconciliation and no session-statistics authority.
- **REG14 — Failure semantics.** See the matrix below; every failure is per-strategy isolated and
  never causes another strategy to fabricate a result.
- **REG15 — Provider-enabled scope.** Strategy enablement is honored only in the provider-enabled
  composition path (a strategy needs live data + warmup, which need the provider). The disabled
  runtime stays dormant (no strategies started), regardless of `strategies_enabled`.
- **REG16 — Out of scope.** Persistence (PostgreSQL / snapshot / result), REST/WebSocket scanner
  transport, React, order execution, and Open=High/Open=Low/Momentum implementations are later slices.

## Failure matrix (REG14)

| Condition | Where caught | Outcome |
|-----------|--------------|---------|
| Duplicate strategy id (catalog/registry) | `StrategyRegistry.register` | `StrategyAlreadyRegisteredError` — composition fails fast |
| Enabled id not in catalog | composition | governed `UnknownEnabledStrategyError` (fail-closed) |
| Invalid / wrong-type configuration | `manager.start` readiness | that strategy → `ERROR` (readiness `MISSING_CONFIGURATION`); others unaffected |
| Strategy startup / warmup-unavailable / not-ready | `manager.start` | that strategy → `ERROR`, isolated & logged; runtime keeps running (ADR-010 D13) |
| Scanner policy exists, strategy absent | scanner | no results arrive → `snapshot` is `None` (inert; not an error) |
| Strategy present, no scanner policy | scanner | strategy runs & publishes, but is not scanner-ranked (unknown-policy → ignored) — allowed |
| Historical warmup unavailable (no dataset) | composition | already fail-fast at composition (ADR-011 dataset-failure policy) — never reaches enablement |

## Production vertical-flow proof (M)
```
authoritative calendar → historical warmup → PreviousSessionFacts → NarrowCprStrategy
  → StrategyEvaluation → StrategyResult → StrategyResultsPublished
  → CrossInstrumentStrategyScanner → cpr_width_pct ASC → ScannerSnapshot
```
**Missing link:** the `NarrowCprStrategy` node — production never constructs/registers it and never
STARTS it, so its `HistoricalRequirement` never enters the union. The implementation slice inserts and
activates that node via the catalog + `strategies_enabled` + the runtime start-enablement step; every
other link already exists and is tested.

## STOP-condition assessment
None triggered: (1) no existing conflicting production registration owner; (2) no `Strategy` protocol
change (reuses `register` + `manager.start`); (3) no current-day/session-statistics authority; (4) no
new per-strategy task (inline awaited `manager.start`, one subscription); (5) Narrow CPR starts without
provider coupling in the strategy/catalog (the provider supplies data via existing governed ports);
(6) configuration ownership is the catalog entry default; (7) no scanner-specific branch (generic
policy registry); (8) no auto-start without the explicit `strategies_enabled` decision.

## Future implementation contract (NARROW-CPR-REGISTRATION-IMPL-R1 — not implemented here)
- Add `app/services/strategy_catalog.py` (broker-neutral): a `StrategyCatalogEntry` (strategy +
  default config + optional `ScannerRankingPolicy`, with REG2 validation) and the production catalog
  containing the `NarrowCprStrategy` entry (config default; policy `cpr_width_pct` ASCENDING).
- Add `Settings.strategies_enabled` (CSV, default empty; validated ids).
- `compose_market_runtime` (enabled path): resolve enabled entries from the catalog, pass their
  `strategies` + `configurations` + `scanner_ranking_policies` to `LiveMarketRuntime` (replacing the
  standalone `_SCANNER_RANKING_POLICIES` literal); fail-closed on an enabled id absent from the catalog.
- `LiveMarketRuntime.start()`: after subscribing, `await manager.start(strategy_id, reference=clock.now())`
  for each enabled strategy, isolating per-strategy `ERROR` (skip + log, not fatal). No new task.
- No change to `NarrowCprStrategy`, the scanner, the manager core, the Market Engine, calendar_data,
  adapters, or authority flags.

## Future test matrix
Catalog entry REG2 validation (policy/config mismatch fails fast); `strategies_enabled` empty ⇒ zero
registered/started ⇒ zero historical demand ⇒ scanner inert; `strategies_enabled=narrow_cpr` (provider
enabled) ⇒ registered + configured + STARTED (RUNNING) ⇒ requirement in union ⇒ warm ⇒ evaluate on
`ON_HISTORICAL_READY` ⇒ result ⇒ scanner ranks it (end-to-end); duplicate id fails fast; unknown enabled
id fails fast; invalid config ⇒ that strategy ERROR, others unaffected; one strategy start failure
isolated (runtime survives); scanner policy without strategy ⇒ snapshot None; strategy without policy ⇒
runs unranked; disabled composition ⇒ no strategies started regardless of the list; `create_task` count
stays 3; provider-neutral import guard on the catalog; `supports_current_day`/authority bits stay False;
no `if strategy_id ==` in generic code; historical/calendar/runtime/scanner regressions green.

## Files changed
Docs-only: this ADR + the README index row. No `app/`/`tests/` change this phase.

## Consequences
**Positive.** A deterministic, explicit, provider-neutral production registration + enablement seam:
Narrow CPR (and future strategies) participate in the live pipeline via a catalog entry + an
enablement flag, with per-strategy failure isolation, no new task, and no generic strategy-id branch.
**Negative / accepted.** Enabling a strategy triggers real historical warmup I/O at startup (the point
of warmup); a per-strategy `ERROR` is silently skipped (ADR-010 D13) — surfaced via logs/status, not a
hard failure. **Neutral.** No code this phase.

## Exact next slice
**NARROW-CPR-REGISTRATION-IMPL-R1** — implement the catalog + `strategies_enabled` + composition
wiring + runtime start-enablement per this ADR and its test matrix. Then, independently, the scanner
API/WebSocket transport slice.
