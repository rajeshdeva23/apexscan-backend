# ADR-012 — Cross-Instrument Strategy Scanner

| Field | Value |
|-------|-------|
| **Status** | Accepted (architecture + contracts); **implementation DEFERRED** to NARROW-CPR-SCANNER-IMPL-R1 |
| **Date** | 2026-08-16 |
| **Deciders** | Strategy / Platform Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Complements** | ADR-004 (208 NSE cash-equity universe), ADR-006 (candle completeness), ADR-007 (strategy lifecycle, results, ranking D10/D11, per-strategy specs D15), ADR-010 (runtime composition & managed-task lifecycle), ADR-011 (authoritative calendar) |
| **Related** | `docs/07_STRATEGY_ENGINE.md` §12/§14/§17, ADR-007 Narrow CPR strategy specification |

---

## Context

`NarrowCprStrategy` (ADR-007 Narrow CPR spec) produces, **per instrument**, a
`StrategyEvaluation` carrying a `cpr_width_pct` metric (smaller ⇒ narrower). The product
needs a **cross-instrument** ranked view over the configured F&O universe (narrowest CPR
first). Repository inspection confirms this surface does not exist:

- **No** cross-instrument scanner / aggregator / snapshot / leaderboard / screener / candidate
  model exists anywhere (`class …(Scanner|Aggregator|Snapshot|Leaderboard|Screener|Candidate)`
  → none; "scanner" appears only in docstrings).
- The existing `rank_results` / `StrategyResultsPublished` (`app/strategy_manager/`) is
  **per-instrument, across strategies, descending score**, and its own docstring states
  "there is no global cross-instrument ordering guarantee." That is a *different* concept
  from what Narrow CPR needs (**across instruments, one strategy, ascending `cpr_width_pct`**).
- `StrategyResultsPublished` is published on the shared `EventBus` per instrument per
  MarketContext version, carrying the emitted (deduplicated) `StrategyResult`s;
  **SKIPPED/ERROR "stay internal (never published)"**; the manager already computes
  `trading_date` at publish time but does not put it on the event.
- Canonical instrument identity is `Instrument(exchange, symbol)` (frozen, hashable). The
  universe is the runtime's resolved `tuple[Instrument]` (ADR-010 D5; ADR-004). No
  `StrategyResult` persistence exists (`app/api`/`app/websocket` expose only health/version).

This ADR governs a **generic** scanner surface; it introduces no strategy mathematics and
does not modify `rank_results`.

## Decision (NCRS1–NCRS23)

- **NCRS1 — Generic scanner (Option B).** A broker-neutral `CrossInstrumentStrategyScanner`
  in the service/composition layer (`app/services/`), generic over strategy-provided ranking
  metadata. **No** `if strategy_id == "narrow_cpr"` — no strategy mathematics inside the
  scanner. (Option A Narrow-CPR-specific — rejected, not plug-and-play. Option C external
  policy registry — adopted *for the policy* only, NCRS4. Option D extend `rank_results` —
  rejected: it is a different, per-instrument concept, ADR-007 D11.)
- **NCRS2 — Aggregation input.** The scanner consumes the existing public
  `StrategyResultsPublished` event (a single result pipeline; **no** second/parallel result
  stream). It reads per-instrument emitted `StrategyResult`s and their `metrics`.
- **NCRS3 — Scanner ownership (ADR-010).** Owned by `LiveMarketRuntime`; it **subscribes** to
  the shared `EventBus` on `start()` and **unsubscribes** on `shutdown()`, exactly like
  `StrategyManager`. It is an event *subscriber*, not a task owner → **no new `create_task`**
  (the runtime's three managed tasks are unchanged; ADR-010 lifecycle intact).
- **NCRS4 — Ranking-policy ownership.** A broker-neutral value object
  `ScannerRankingPolicy(metric_name: str, ordering: ASCENDING|DESCENDING)` is registered in a
  composition-layer `ScannerRankingPolicyRegistry` keyed by `strategy_id`. It lives **outside**
  the pure `evaluate()` contract and **off** `StrategyDescriptor` (which stays pure identity).
  The generic scanner reads the policy to know which metric to rank by and in which direction —
  never hardcoding it. (Considered alternative: a strategy-declared `ranking_policy` property on
  the `Strategy` protocol — deferred; it would change the shared protocol. The external registry
  needs no protocol change.)
- **NCRS5 — Narrow CPR ranking metric.** `metric_name = "cpr_width_pct"`, read from
  `StrategyResult.metrics` by name (Decimal). No fake score is manufactured; `StrategyEvaluation.score`
  stays `None` (ADR-007 Narrow CPR spec NCR11/§15).
- **NCRS6 — Ordering direction.** Narrow CPR ranks **ascending** by `cpr_width_pct` (smallest
  width = rank 1). The policy carries the direction so descending-metric strategies (e.g.
  momentum) are supported without scanner changes.
- **NCRS7 — Deterministic tie-break.** Ties on the metric break by **ascending canonical
  instrument identity** `(exchange, symbol)`, yielding a *total* order independent of
  async/arrival/dict ordering.
- **NCRS8 — Eligibility (rank + account).** Ranked candidates = **MATCHED** results carrying
  the ranking metric. **NO_MATCH** results (e.g. above a configured threshold) are recorded as
  *evaluated but not eligible* (for completeness accounting), not ranked. SKIPPED/ERROR never
  publish → absent (never fabricated). The scanner is a rank-and-account hybrid, not a pure filter.
- **NCRS9 — Snapshot identity.** A snapshot is keyed by
  `(strategy_id, strategy_version, config_version, trading_date, universe_identity)`. This
  requires `trading_date`, which the manager already computes at publish time, so
  **`StrategyResultsPublished` is extended with `trading_date: date | None`** (a minimal, honest
  payload enrichment — not a second pipeline, not a `StrategyEvaluation` change). `universe_identity`
  is the runtime's resolved universe (one per runtime instance; rebuilt on restart, ADR-010 D5).
- **NCRS10 — Completeness.** `ScannerSnapshotCompleteness ∈ {PARTIAL, COMPLETE}`. COMPLETE iff
  `evaluated_count == expected_count` (every expected instrument published a terminal MATCHED/NO_MATCH).
  Because SKIPPED/ERROR/not-ready never publish on the single public pipeline, snapshots are normally
  **PARTIAL** until every instrument-with-history reports — an honest state, not an error. No
  BUILDING/FAILED states are invented (per-instrument isolation makes whole-scan FAILED meaningless).
- **NCRS11 — Partial-result policy.** The current snapshot is always readable and explicitly
  labelled PARTIAL vs COMPLETE. V1 serves the current snapshot (the product surfaces rankings as they
  fill in); PARTIAL is distinguishable from COMPLETE.
- **NCRS12 — Failure isolation.** A per-instrument SKIPPED/ERROR/outside-coverage/missing-history/
  malformed outcome simply leaves that instrument **absent** from the snapshot — never `width=0`,
  never a fabricated candidate, never a score. One instrument never fails the whole scan (the manager
  already isolates per-instrument, ADR-007).
- **NCRS13 — Dedup / idempotency.** One canonical candidate slot per instrument per snapshot key.
  Upstream `EmissionDeduplicator` + `EmissionPolicy.ONE_SHOT_PER_SESSION` (Narrow CPR) already emit
  ≤1 material result per `(strategy, instrument, trading_date)`. The scanner keeps the latest
  published result per instrument for the active key (replacement only on a higher `context_version`
  for the same key).
- **NCRS14 — Bounded state.** Current snapshot only (≤ universe-size candidates). A new snapshot key
  (new trading date / config / universe) replaces the previous. No unbounded in-memory history.
- **NCRS15 — Event publication.** V1 exposes the current snapshot as **bounded in-memory state with a
  read accessor**; it does **not** publish a new per-instrument `ScannerSnapshotPublished` event (which
  would be a noisy second stream). Push/event transport is decided with the API/WebSocket slice (NCRS19).
- **NCRS16 — Historical-demand ownership.** Strategy-owned. The scanner issues **no** historical/market
  requests. Narrow CPR's `HistoricalRequirement(session, 1)` drives warmup through the existing
  `RequirementsCoordinator` / `HistoricalCoordinator` / `HistoricalCache` (bounded concurrency,
  request coalescing) only while the strategy is RUNNING. The scanner is a pure results consumer.
- **NCRS17 — Concurrency / task policy.** Event-driven aggregation inside the subscriber handler
  (synchronous, like the manager). **No** per-instrument/per-candidate/per-result task; no new
  `create_task`. Insertion is O(1); ordering is O(N log N) on read/finalize — trivial vs historical
  acquisition for hundreds of instruments.
- **NCRS18 — Provider neutrality.** The scanner imports no Dhan / httpx / websocket / broker type; it
  consumes only domain objects (`StrategyResult`, `Instrument`, `MetricEntry`). It never sees a
  provider payload.
- **NCRS19 — API/WebSocket boundary.** This slice governs the scanner **domain/service only**.
  API/WebSocket exposure (read endpoint and/or push) is a separate NARROW-CPR-SCANNER-API slice.
- **NCRS20 — Persistence boundary.** V1 is in-memory only (no `StrategyResult` persistence exists; the
  contract guard forbids manager persistence). Durable scanner history is a separate future slice; V1
  adds no DB tables.
- **NCRS21 — Plug-and-play future strategy.** A future strategy becomes scannable by (a) emitting its
  ranking metric in `StrategyResult.metrics` and (b) registering a `ScannerRankingPolicy(metric, ordering)`
  — **no scanner code change**. Open=High / Open=Low register their own metric + ordering; a momentum
  scanner registers a descending policy. The generic (metric + direction + instrument tie-break) ordering
  covers all.
- **NCRS22 — Determinism / replay.** Identical `{calendar dataset version, historical OHLC, universe,
  strategy version, configuration, evaluations}` ⇒ an identical ranked snapshot. Ordering is a total order
  (metric direction, then instrument identity); independent of async/arrival/dict order; no wall-clock,
  network, or randomness in the scanner.
- **NCRS23 — Current-day / authority / directionality isolation.** The scanner grants no authority:
  `supports_current_day=False`, `staged_observation_verified=False`, `tick_aggregate_verified=False`
  unchanged; Narrow CPR stays historical-only. The scanner is **non-directional** — rank #1 means only
  "narrowest CPR by the governed metric," never BUY/SELL/bullish/bearish.

## Governed flow
```
HistoricalWarmup → InstrumentStateRegistry → StrategyManager → NarrowCprStrategy.evaluate()
  → StrategyResult (per instrument) → StrategyResultsPublished (EventBus)
  → CrossInstrumentStrategyScanner (subscriber) → ScannerSnapshot (bounded, ranked)
  → [read accessor] → API/WebSocket/persistence (later slices)
```
The strategy owns CPR math + `cpr_width_pct`; the scanner owns collection, eligibility, ordering,
tie-breaking, and snapshot semantics.

## Candidate result model (canonical, refined at IMPL)
- `ScannerRankingPolicy(metric_name, ordering)` + a `ScannerRankingPolicyRegistry` (strategy_id → policy).
- `ScannerCandidate(instrument, strategy_id, strategy_version, status, rank, ranking_metric_name,
  ranking_metric_value, metrics)`.
- `ScannerSnapshot(strategy_id, strategy_version, config_version, trading_date, universe_identity,
  expected_count, evaluated_count, eligible_count, candidates, completeness)`.
- `ScannerSnapshotCompleteness ∈ {PARTIAL, COMPLETE}`.

## STOP-condition assessment
None triggered: (1) no incompatible existing scanner; (2) no `StrategyEvaluation` semantic change
(only a `trading_date` field added to the *publication event*); (3) no fake score (metric-based);
(4) no provider data; (5) no current-day authority; (6) no per-instrument unmanaged tasks
(event-driven); (7) deterministic total order from canonical `Instrument` identity; (8) trading-date
determined honestly via the event enrichment the manager already computes; (9) PARTIAL/COMPLETE is the
new snapshot type's own vocabulary, not a new state on an existing contract.

## Future implementation contract (NARROW-CPR-SCANNER-IMPL-R1 — not implemented here)
- Add `app/services/cross_instrument_scanner.py` (scanner service + `ScannerSnapshot`/`ScannerCandidate`/
  `ScannerRankingPolicy`/registry/completeness). Extend `StrategyResultsPublished` with `trading_date`
  and set it in the manager's `_publish_results` (it already computes it). Subscribe/unsubscribe the
  scanner in `LiveMarketRuntime` (subscription only — **no new task**). Register Narrow CPR's policy
  (`cpr_width_pct`, ASCENDING) at composition. No change to `rank_results`, historical algorithms,
  calendar, adapters, authority flags, or current-day support.

## Future test matrix
Collect across instruments; ascending-width ranking; deterministic tie-break; arrival-order invariance;
MATCHED/NO_MATCH/SKIPPED/ERROR handling; missing/malformed metric fails closed (candidate excluded, never
scored); dedup idempotency + replacement; expected-universe count; complete vs partial snapshot;
one-instrument failure isolation; zero/one eligible candidate; hundreds-of-instruments synthetic;
strategy-disabled ⇒ no scanner demand; no extra historical requests; no per-instrument tasks; bounded
memory; deterministic replay; provider-neutral import guard; no direction manufactured; Narrow CPR score
stays None; authority/current-day flags stay False; historical/calendar/manager/architecture/contract
regressions green.

## Consequences
**Positive.** A generic, provider-neutral, deterministic cross-instrument scanner surface that ranks any
metric-emitting strategy via a registered ranking policy — Narrow CPR ships ascending by `cpr_width_pct`
with no fake score and no directional claim, reusing the single result pipeline and adding no runtime task.
**Negative / accepted.** Snapshots are normally PARTIAL because SKIPPED/ERROR/not-ready instruments do not
publish on the public event; a definitive COMPLETE accounting of non-published outcomes would need an
internal-record consumer (a separate future decision). API/WebSocket transport and durable history are
deferred. **Neutral.** No code this phase.

## Exact next slice
**NARROW-CPR-SCANNER-IMPL-R1** — implement the generic scanner + ranking-policy registry + the
`trading_date` event enrichment + Narrow CPR policy registration, per this ADR and its test matrix.
Then NARROW-CPR-SCANNER-API-R1 (transport) and, if needed, a persistence/history slice.
