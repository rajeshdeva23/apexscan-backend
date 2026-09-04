# ADR-019 — Session-Aware Live-Ingestion Supervision (Terminal-Failure Self-Healing)

| Field | Value |
|-------|-------|
| **Status** | Proposed |
| **Date** | 2026-09-04 |
| **Deciders** | Platform / Market Data Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Refined by** | — |
| **Related** | ADR-010 (runtime composition & managed lifecycle), ADR-006 (feed continuity / adapter reconnect), `docs/sector_view/` (SECTOR-VIEW-1C-R3 evidence) |

---

## Context — the failure discovered by SECTOR-VIEW-1C-R3

On 2026-09-03/04 the production runtime exhibited a silent, unrecoverable outage:

- The container stayed healthy, `RestartCount=0`, and the Dhan access token stayed valid
  (a REST call succeeded the next morning).
- The Dhan live WebSocket dropped and the adapter's **bounded** reconnect (ADR-006, 3 attempts)
  was exhausted at ~23:55 IST, raising `ProviderUnavailableError`.
- `market_runtime` logged `live ingestion task ended unexpectedly (ProviderUnavailableError)`
  and **nothing restarted it**. The `_on_ingestion_done` callback only recorded a fatal flag.
- The application then ran all of the next session with **zero** live market data. Sector
  Shadow observed 0/210.

Root cause (proven from source): the ingestion task (`_run_ingestion`, one `async for` over
`adapter.stream_market_data`) had **no supervisor**. Adapter-level reconnect is bounded by
design; once it is exhausted the stream terminates, and the runtime had no layer to resume it.

## Decision

Introduce a **session-aware ingestion supervisor** in the runtime/service layer, above the
provider adapter (`LiveMarketRuntime._supervise_ingestion`). Application lifecycle policy lives
here; provider-specific reconnect mechanics stay inside the adapter (ADR-006).

**Ownership.** `start()` launches exactly one supervisor task (replacing the bare ingestion
task). The supervisor consumes one stream at a time by `await`-ing `_run_ingestion()` inline —
never a second concurrent task — so **at most one ingestion owner** exists by construction.

**Self-healing.** When a stream terminates unexpectedly (e.g. `ProviderUnavailableError`, or an
abnormal return), the supervisor schedules a bounded recovery instead of leaving ingestion dead.
It never gives up permanently while the runtime is started and a provider is configured.

**Session-aware recovery.** Recovery consults the authoritative `MarketSessionClassifier`:
- Phases expecting live data (PRE_OPEN, OPENING_AUCTION, LIVE_SESSION, CLOSING_SESSION): retry
  promptly under bounded backoff.
- Otherwise (MARKET_CLOSED / HOLIDAY / EMERGENCY_HALT / CALENDAR_UNAVAILABLE): enter
  `WAITING_FOR_SESSION` and poll at a calm cadence — **no overnight reconnect/token storm**.
- On transition into a live-data phase, ingestion resumes **without a container restart** (the
  invariant the R3 outage violated).

**Bounded backoff.** `IngestionRecoveryPolicy` gives jittered exponential backoff
(default 1s → cap 30s) across *consecutive* failed runs; a run that received ≥1 event resets it.
No tight loop, no unbounded task creation. Sleep/clock/jitter are injected for deterministic
tests and never block shutdown.

**State machine.** `IngestionState`: STOPPED → STARTING → RUNNING → (RECOVERING |
WAITING_FOR_SESSION) → RUNNING … ; cancellation (shutdown) → STOPPED.

## Authentication safety (critical — governed, rate-limited token generation)

Proven from source: recovery reuses the **existing** provider/adapter instance and re-invokes
`stream_market_data`. The adapter's reconnect calls `DhanAuthManager.get_access_token()`, which
returns the **cached** in-memory token while usable and regenerates only when it is absent or
near expiry. The supervisor never rebuilds the provider and never calls `disconnect()` (which
would clear the token). Therefore repeated recovery attempts during a session cause **no token
generation** (the cached token is reused); a fresh generation happens only if the token has
genuinely expired — bounded to one, not a storm. Authentication governance is unchanged.

## Health semantics

Provider health remains adapter-driven (`_live_status` via the coordinator). During
recovery/waiting it stays degraded/unhealthy; it returns healthy only after a genuine reconnect
+ subscription reconciliation. The runtime never reports `ingestion_running` while merely
recovering. This preserves the correct behavior R3 already showed (`/health/ready` went
unhealthy).

## Failure isolation

Supervision is infrastructure-only. It executes no strategies, enables no trading, places no
orders, writes no sector data, and never touches SessionStatisticsAuthority (ADR-009), the R4D
evidence observer, or sector calculations.

## Observability

`LiveMarketRuntime.ingestion_diagnostics()` exposes bounded internal counters: state,
ingestion/recovery running flags, last RX / last failure timestamp + type, recovery attempts,
successful recoveries, consecutive failures, last recovery attempt, and next-recovery-not-before.
No public API and no credentials are exposed.

## Consequences

- The R3 outage class (terminal WS failure → permanent silent death) is eliminated: ingestion
  self-heals within a session and resumes at the next session without a restart.
- Bounded, session-aware, auth-safe, single-owner — no reconnect/token/crash storms.
- Boundary preserved: the runtime depends on provider abstractions only; provider reconnect
  stays in the adapter.

## Status note

Proposed. Offline implementation only — not deployed. Accepted ADRs are unchanged; this refines
the ADR-010 managed-lifecycle model.

## Known limitations

- Recovery cannot succeed while the provider endpoint itself is unavailable (e.g. Dhan WS
  refusing connections); the supervisor keeps retrying under bounded backoff and resumes when
  the endpoint accepts again — it cannot manufacture a feed.
- Session gating uses the same schedule/calendar as the classifier; a calendar-unavailable
  window defers recovery (fail-safe) rather than forcing a connection.
