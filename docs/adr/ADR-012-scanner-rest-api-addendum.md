# ADR-012 Addendum — Cross-Instrument Scanner REST Transport (V1)

| Field | Value |
|-------|-------|
| **Type** | Subordinate governance addendum (not a numbered ADR) |
| **Subordinate to** | ADR-012 — Cross-Instrument Strategy Scanner |
| **Related** | ADR-010 (runtime composition & lifecycle), ADR-013 (strategy registration/enablement), ADR-003 (provider neutrality), ADR-007 Narrow CPR strategy specification |
| **Status** | Accepted (transport contract); **implementation DEFERRED** to NARROW-CPR-SCANNER-API-IMPL-R1 |
| **Date** | 2026-08-16 |
| **Deciders** | Platform / API Architecture |
| **Decision** | Expose `ScannerSnapshot` over a **generic, read-only REST endpoint** — `GET {api_v1_prefix}/scanners/{strategy_id}` — reading the lifecycle-owned in-memory scanner state. WebSocket, persistence, and auth stay out of scope. |

---

## Context

The scanner (ADR-012) holds a bounded, deterministic, in-memory current `ScannerSnapshot` per
strategy and exposes `LiveMarketRuntime.scanner_snapshot(strategy_id)`; Narrow CPR ranks ascending
by `cpr_width_pct` (rank 1 = narrowest). No transport exists. Repository inspection:

- FastAPI, versioned router mounted at `settings.api_v1_prefix` (`/api/v1`); endpoints reach the
  owner via `request.app.state.lifecycle` (`ApplicationLifecycle`). Health/readiness/startup use
  `JSONResponse(status_code=200|503, content=snapshot.as_dict())`. **No scanner/result endpoint.**
- `app/websocket/__init__.py` is an **empty placeholder** (Redis-pub/sub design deferred) — there is
  **no** WS transport, so WS would be a new subsystem.
- The scanner is owned via `ApplicationLifecycle._provider` (`LiveMarketRuntimeDependency` →
  `RuntimeComposition.runtime.scanner_snapshot`) but has **no public accessor**; `main.py` wires the
  runtime dependency only when `market_provider_enabled` (else `provider=None`).
- Decimal has never been serialized to JSON in this API; there is no existing Decimal-JSON convention.

## Decisions API1–API22

- **API1 — Transport V1: REST only.** A synchronous GET reads existing in-memory state. WebSocket
  (no existing WS transport) and SSE (no existing pattern) are **deferred**; building either now
  would add a new delivery subsystem for no current need.
- **API2 — Generic endpoint.** `GET {api_v1_prefix}/scanners/{strategy_id}` — path-generic over the
  strategy id, so `narrow_cpr`, `open_high`, `open_low`, `momentum`, … reuse it with **no** new
  endpoint code and **no** `narrow_cpr`-specific route.
- **API3 — Response schema.** An API-safe pydantic projection of `ScannerSnapshot`:
  `strategy_id, strategy_version, config_version, trading_date, expected_count, evaluated_count,
  eligible_count, completeness, candidates[]`; each candidate = `rank, instrument{exchange, symbol},
  ranking_metric_name, ranking_metric_value, status`. It is a projection — the API never recomputes
  ranking or metrics.
- **API4 — Decimal serialization: JSON string.** `ranking_metric_value` (and any Decimal) serialize
  as a **string** (e.g. `"0.03125"`) — precision-preserving, never through a binary float. This
  addendum sets the repo's Decimal-over-JSON convention (there was none).
- **API5 — Instrument shape.** Canonical `{exchange, symbol}` only. **No** provider `security_id`,
  `exchange_segment`, or any provider payload field (ADR-003).
- **API6 — Candidate order preserved.** The response emits candidates in the scanner's rank order
  verbatim; the API **never re-sorts**. For narrow_cpr, rank 1 = smallest `cpr_width_pct`.
- **API7 — PARTIAL/COMPLETE exposed.** `completeness` is returned verbatim from the snapshot; never
  derived from a clock. Clients can distinguish PARTIAL from COMPLETE.
- **API8 — Non-directional.** No `BUY`/`SELL`/`LONG`/`SHORT`/bullish/bearish field or language. Rank
  means "narrowest first" only; any descriptive text stays neutral.
- **API9 — Known scanner strategy, no ranked snapshot yet.** `200` with `snapshot: null` (a
  scanner-enabled strategy that has not yet produced a snapshot — e.g. not started, or no results
  this session).
- **API10 — Unknown / non-scanner-enabled strategy.** `404` (no registered `ScannerRankingPolicy`
  for the id). Distinguishing this from API9 requires a scanner "scannable ids" read (see API15).
- **API11 — Provider-disabled / runtime not composed.** `503` (reusing the dependency/readiness
  error model). Never fabricate scanner data. When `market_provider_enabled=False` there is no
  runtime dependency, so every scanner GET is `503`.
- **API12 — Runtime down / failed.** `503` — a failed/not-started runtime must not serve a snapshot
  as if fresh (§15). The scanner-source accessor reports unavailable unless the runtime is composed
  and started.
- **API13 — `limit` projection.** Optional `?limit=N` with `1 ≤ N ≤ 500` (a bounded cap above the
  ~208 F&O universe); returns the top-`N` candidates **by existing rank** (projection only — it
  never changes scanner state or ranking). Default: all candidates. An out-of-range `limit` → `422`
  (FastAPI validation). **No** other filters in V1 (no min/max width, symbol, sector, direction).
- **API14 — Strategy lifecycle status is not the scanner's concern.** The scanner endpoint does
  **not** expose REGISTERED/RUNNING/PAUSED/ERROR/STOPPED; that belongs to a separate future
  strategy-status endpoint. The candidate `status` field is the per-instrument `EvaluationStatus`
  (MATCHED) carried by the snapshot, not the strategy lifecycle state.
- **API15 — Runtime DI / access (smallest safe seam).** The endpoint reads
  `request.app.state.lifecycle`; `ApplicationLifecycle` gains a read-only `provider` property
  returning the `ProviderDependency` it already owns (core.lifecycle stays scanner-agnostic — it
  imports no scanner type). The API narrows it via a `@runtime_checkable ScannerSnapshotSource`
  Protocol — `scanner_snapshot(strategy_id) -> ScannerSnapshot | None` and
  `scannable_strategy_ids() -> frozenset[str]` — implemented by `LiveMarketRuntimeDependency`,
  delegating to the composed, started runtime (else reporting unavailable). **No** second runtime,
  provider, or global singleton is created; the request never composes anything.
- **API16 — Caching.** `Cache-Control: no-store` (intraday current-session state); **no** Redis
  caching.
- **API17 — Security.** Unauthenticated, like the existing health/version endpoints (the codebase
  has no auth layer yet); if one is later added, the scanner endpoint adopts it. It never exposes
  access tokens, client id, PIN, TOTP, security ids, or provider payloads.
- **API18 — Provider neutrality.** The endpoint and its read path consume only the broker-neutral
  `ScannerSnapshot`/`Instrument`; no provider type or identity appears in the response or the
  accessor chain.
- **API19 — Determinism / freshness.** Repeated GET on unchanged scanner state returns an identical
  body. **No** wall-clock `generated_at` field is added (it would break determinism and §16);
  `trading_date` + the counts + `completeness` are the snapshot's temporal identity.
- **API20 — Performance.** The GET is in-memory and bounded: no provider call, no historical call,
  no strategy evaluation during request handling — only the scanner's deterministic `snapshot()`
  read (an `O(N log N)` sort over ≤ universe candidates).
- **API21 — WebSocket / persistence boundary.** Both deferred (WebSocket → NARROW-CPR-SCANNER-WS-GOV-R1;
  durable history → a later persistence slice). **No** `ScannerSnapshotPublished` event is added for
  REST. PostgreSQL is **not** required and **not** a blocker.
- **API22 — Current-day / authority isolation.** `supports_current_day=False`,
  `staged_observation_verified=False`, `tick_aggregate_verified=False` unchanged; the response carries
  no authority/provider-verification/calendar-mutation flags.

## Endpoint behaviour matrix
| Situation | HTTP | Body |
|-----------|------|------|
| Provider disabled / no runtime dependency | 503 | error (dependency model) |
| Runtime composed but not started / failed | 503 | error |
| Unknown / non-scanner-enabled `strategy_id` | 404 | error |
| Scanner-enabled, no ranked snapshot yet | 200 | `snapshot: null` |
| Scanner-enabled, ranked snapshot present | 200 | projected `ScannerSnapshot` (rank order preserved) |
| `limit` out of `[1, 500]` | 422 | validation error |

## STOP-condition assessment
None triggered: (1) the runtime is reachable via a small read-only accessor (no second runtime/
singleton); (2) the scanner read is pure in-memory (no provider I/O); (3) ranking semantics are
untouched (projection only); (4) no provider identity is exposed; (5) runtime-disabled maps to the
existing `503` dependency model; (6) WebSocket is deferred, so no new background-task model is
introduced in this slice.

## Future implementation contract (NARROW-CPR-SCANNER-API-IMPL-R1 — not implemented here)
- `app/schemas/scanner.py` (or the endpoint module): frozen pydantic `ScannerSnapshotResponse` /
  `ScannerCandidateResponse` with Decimal-as-string serialization (API4).
- `app/api/v1/endpoints/scanners.py`: `GET /scanners/{strategy_id}` with the API9–API13 behaviour and
  `Cache-Control: no-store`; register it in `app/api/v1/router.py`.
- `app/core/lifecycle.py`: a read-only `provider` property on `ApplicationLifecycle` (no scanner import).
- Scanner-read seam: a `@runtime_checkable ScannerSnapshotSource` Protocol (services layer);
  `CrossInstrumentStrategyScanner.scannable_strategy_ids()` (policy-registry keys) +
  `LiveMarketRuntime.scannable_strategy_ids()`; `LiveMarketRuntimeDependency` implements
  `scanner_snapshot`/`scannable_strategy_ids` (unavailable unless composed + started).
- Tests: `tests/unit/test_scanner_api.py` via the FastAPI test client. **No** change to the scanner
  ranking, strategies, Market Engine, calendar_data, adapters, authority flags, or a new task.

## Future test matrix
A known-strategy snapshot → 200; B candidate order preserves rank; C narrow_cpr rank 1 narrowest;
D Decimal serialized as an exact string; E PARTIAL exposed; F COMPLETE exposed; G `limit` projects
top-N by rank; H `limit` validation (422); I known-but-empty → 200 null; J unknown → 404; K
provider-disabled → 503; L runtime-unhealthy → 503; M no provider call during GET; N no historical
call during GET; O no new scanner/runtime object per request; P no directional field; Q no
security/provider ids; R `trading_date` serialized correctly; S Instrument `{exchange, symbol}`
only; T `score=None` does not prevent ranking/response; U a fake second strategy uses the same
endpoint; V no narrow_cpr branch in the endpoint; W authority flags unchanged; X architecture/
contract guards; Y full API regressions; Z deterministic repeated GET.

## Files changed
Docs-only: this addendum + the README index row. No `app/`/`tests/` change this phase.

## Consequences
**Positive.** A generic, provider-neutral, deterministic read-only REST surface for any
metric-emitting strategy's ranked snapshot — Narrow CPR is reachable at
`/api/v1/scanners/narrow_cpr` with rank order preserved, PARTIAL/COMPLETE explicit, zero provider/
historical work per request, and no new runtime/task/singleton. **Negative / accepted.** No push
updates in V1 (clients poll); no durable history; unauthenticated (matches current API). **Neutral.**
No code this phase.

## Exact next slice
**NARROW-CPR-SCANNER-API-IMPL-R1** — implement the generic `GET /scanners/{strategy_id}` endpoint +
response schema + the read-only runtime accessor per this addendum and its test matrix. Then
**NARROW-CPR-SCANNER-WS-GOV-R1** (push transport) and a persistence/history slice.
