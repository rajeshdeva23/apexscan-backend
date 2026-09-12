# ADR-025 — Decoupled Market Ingestion Service and Authority Cutover (Phase-H Design)

| Field | Value |
|-------|-------|
| **Status** | Proposed (DESIGN / READINESS GATE — no activation) |
| **Date** | 2026-09-13 |
| **Deciders** | Platform / Market-Ingestion Decoupling Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | ADR-006 (feed continuity fact), ADR-010 (runtime composition), ADR-020 (M1), ADR-021 (D1), ADR-022 (M2), ADR-023 (C1), ADR-024 (L1); DESIGN-REVIEW-2 |

> This ADR is a **design and readiness gate only**. It implements nothing, activates nothing,
> and does not contact production. It freezes service ownership, the authoritative-sink
> requirement, the rollout mode/flag matrix, and the shadow→cutover→rollback plan, and records
> blocker status. Every production-activating step is a later, separately-authorized subphase.

---

## Context

The decoupling foundation is complete on `feature/decoupling-hardening` (`d0bb52b`): M1 durable
producer identity, D1 atomic stream+reference publication, M2 bounded async publication boundary,
C1 durable consumer idempotency, L1 broker-neutral continuity. All are off by default and not
composed. Production (`main` `a6b8c68`) still runs the monolithic backend-owned Dhan path.

Goal: design the production-safe transition to a two-service topology —
`apexscan-market-ingestion → Redis → apexscan-backend` — where a backend restart no longer
severs or re-authenticates the Dhan connection.

## Current ownership (verified, `feature/decoupling-hardening`)

| Concern | Owner | Evidence |
|---|---|---|
| Dhan provider | `DhanRestAdapter` | `adapters/dhan/adapter.py:172`; built in `services/dhan_runtime_composition.py:508` |
| Dhan auth/token | `DhanAuthManager` (lazy, in-process cache, instance-scoped) | `adapters/dhan/auth.py:93,146-172`; hazard: cache dies with the adapter |
| WebSocket | `DhanRestAdapter` + `WebsocketsDhanLiveTransport` | `adapter.py:643-661`; `adapters/dhan/live.py:153-165` |
| Framing/decoder | `adapters/dhan/live.py` pure fns | `iter_standard_live_packets:302`, `decode_standard_live_packet:274` |
| Canonical event construction | `live.py` decoders → `Tick/Quote/MarketReference` | `live.py:330,373,457` |
| TickEngine / MarketContext | `LiveMarketRuntime` | `services/market_runtime.py:454` |
| IPC publisher (M2/D1) | **not composed** (param exists, never passed) | `market_runtime.py:329`; `compose_market_runtime` never passes it |
| IPC consumer (C1) | **not composed** | no construction outside tests |
| C1 durable dedup / L1 continuity | `market_ipc/durable_dedup.py`, `market_ipc/continuity.py` | not composed |
| App lifecycle | `ApplicationLifecycle` (DB→Redis→provider) | `core/lifecycle.py:96,145-156` |

The Dhan-specific surface is entirely under `adapters/dhan/*` + the provider half of
`dhan_runtime_composition.py`; everything above the `ProviderDependency` protocol
(`core/lifecycle.py:39`) is already broker-neutral. Canonical `MarketData` types
(`schemas/market_data.py`) are the natural inter-service wire contract.

## Target ownership (frozen)

**`apexscan-market-ingestion`** owns only what must stay connected to Dhan independently of
backend restarts: Dhan auth/token lifecycle, Dhan WebSocket + reconnect, framing/decoder,
canonical event construction, M1 producer identity/epoch, M2 publication boundary, D1 atomic
stream+reference publication, L1 producer/publication continuity. Holds `dhan.env`.

**`apexscan-backend`** owns: Redis stream consumption (XREADGROUP/XACK), C1 durable dedup,
TickEngine/MarketContext, sector intelligence, strategies, HTTP APIs, frontend WebSocket. Does
**not** hold Dhan credentials after cutover.

**Redis** — ingestion is the sole writer (XADD / D1 Lua / reference HASH); backend is the sole
consumer/ACKer and dedup-key writer. No overlapping hidden writers. **Durability today is
under-specified (B11):** compose sets only `--appendonly yes`, so RDB save-points remain at
defaults and `appendfsync` is the default `everysec` — a Redis crash can lose the last ~1 s of
already-confirmed writes, and producer-side L1 cannot see that tail loss (see B11 / consume-side
loss detection). Phase H must pin the `appendfsync`/RDB policy in a `redis.conf` (none exists
today) before authoritative activation.

## Rollout modes (one-way progression; never legacy→cutover directly)

| Mode | Dhan owner | Backend authority | Notes |
|---|---|---|---|
| **0 LEGACY_ONLY** | backend | in-process Dhan | current production |
| **1 INGESTION_SHADOW_PUBLISH** | ingestion **and** backend | in-process Dhan | ingestion publishes IPC; **two Dhan owners** — see B6 |
| **2 SHADOW_CONSUME_COMPARE** | ingestion **and** backend | in-process Dhan (authoritative); IPC → shadow sink | compare IPC vs legacy |
| **3 IPC_AUTHORITATIVE_BACKEND** | ingestion only | IPC → authoritative sink | backend `market_provider_enabled=false` |
| **4 LEGACY_PROVIDER_REMOVED** | ingestion only | IPC only | backend Dhan code removed |

## Flag matrix

Independent flags (no single ambiguous boolean):
`market_ingestion_service_enabled` (deploy), `ipc_publisher_enabled` (ingestion),
`ipc_consumer_enabled` (backend), `ipc_shadow_compare_enabled` (backend),
`ipc_authoritative_enabled` (backend), `legacy_market_path_enabled` (backend =
`market_provider_enabled`).

| Mode | legacy | publisher | consumer | shadow | authoritative |
|---|---|---|---|---|---|
| 0 | T | F | F | F | F |
| 1 | T | T | F | F | F |
| 2 | T | T | T | T | F |
| 3 | F | T | T | opt | T |
| 4 | F(removed) | T | T | F | T |

**Illegal combinations — fail fast at startup:**
- `authoritative=T ∧ consumer=F` → no source for authority.
- `authoritative=T ∧ legacy=T` → **dual authority into TickEngine** (forbidden).
- `shadow=T ∧ consumer=F` → nothing to compare.
- `publisher=T ∧ market_ingestion_service_enabled=F` → publisher without a producer.
- `legacy=F ∧ authoritative=F` → backend would have no market authority.
- `consumer=T ∧ shadow=F ∧ authoritative=F` → a consumer with no meaningful sink would still drain
  the stream and **record dedup keys**, silently poisoning dedup so a later `authoritative=T`
  permanently skips those events. Fail fast.
- `authoritative=T` while C1 unavailable / consumer unhealthy / backlog critical → **fail-closed
  readiness** (not a config error, a runtime gate; see B7).

**Cutover dedup namespace (decision required at H8):** the authoritative sink MUST reuse the same
`dedup_key_prefix` the shadow consumer used, so that events already shadow-consumed (and dedup-
recorded) before the authority switch are **suppressed**, giving a clean warm start. A fresh
namespace would **replay the entire retained backlog** into authority at cutover. This choice is
correctness-critical and is frozen here as "shared namespace = warm-start."

## Authoritative sink contract

Define an `apply(event_identity, event)` authoritative sink distinct from the existing
`ShadowMarketEventSink` (compare-only). Required properties: duplicate-safe, restart-safe,
deterministic, explicit failure, **no ACK before safe application**.

**Duplicate safety is already substantially met by the engine (verified):**
- TickEngine applies market values as **replace / max / min / cumulative-delta with no local
  accumulators** — `tick_engine.py:275-279`, `candle_engine.py:354-365,416-424`
  (`bucket.last_cumulative` is a replaced snapshot; interval volume is a delta, never `+=`).
  Re-applying an identical event corrupts **no market value** (category B: monotonic
  reducer / snapshot-replace).
- Tick/Quote have a DUPLICATE (value-equality) + STALE (timestamp-watermark) gate
  (`validation.py:41-45,74-78`).
- C1 dedups by canonical `(producer_id, epoch, sequence)` **before** the sink applies, so ordinary
  redelivery never reaches apply twice.

**Residual edges to design around (do not assume away):**
- **Ordinals churn on re-apply.** Each accepted datum mints a fresh `sequence`/`version`
  (`sequence.py:34`, `context.py:393`), so a re-apply through the apply→mark crash window yields
  an observably different context (same values, new version) — benign churn, not corruption.
- **`MarketReference` has no engine dedup/stale gate** (`tick_engine.py:284-334`). Normal
  redelivery is still suppressed by C1; only the apply→mark crash window can double-apply a
  reference, which is value-convergent (previous_close is a replace).

**ACK ordering (authoritative mode):** validate → C1 `contains` → sink apply → C1 `record` → ACK.
The C1 consumer already implements apply-then-record-then-ACK with fail-closed on dedup-store
error (`consumer.py`); poison-event terminal-ACK policy stays separate and unchanged.

## Lifecycle

**Ingestion startup (ordered, fail-closed):** config validation → M1 durable epoch allocation →
Redis connectivity → D1 publisher construct → M2 boundary `start()` (allocates epoch, fail-closed
before accepting submits) → L1 incarnation start → Dhan auth → Dhan WS connect → subscribe
universe → READY. **Never connect Dhan before IPC publish infrastructure is ready** (canonical
events must have a safe destination).

**Backend consumer startup:** Redis reachable → C1 dedup available → consumer group ensured →
pending-entry recovery → consumer starts → sink (shadow or authoritative) ready. **Never consume
authoritatively before the sink and C1 are ready.**

**Backend restart (mid-session):** ingestion stays connected and publishing; Redis accumulates;
backend reloads `md:reference:<date>` (previous_close + session-OHLC snapshot via
`ReferenceStateLoader`), resumes the consumer (pending recovery + forward read), C1 suppresses
duplicates, TickEngine/MarketContext catch up. **No Dhan reconnect.** Transient memory-only state
not in D1 — candle partials, the volume baseline, version/sequence numbering, and the
dedup/staleness watermark — rebuilds forward. Explicit warm-start inaccuracies to accept and
verify as H6 acceptance criteria: (a) the **first post-restart interval volume** is `None` until a
baseline reforms; (b) the **in-progress candle's OHLC** is rebuilt forward, so its open/high/low
can be wrong for up to one interval (`candle_engine.py:340-365`); (c) the loaded **session-OHLC
re-enters authoritative engine state only if `SessionStatisticsAuthority` is enabled** — it is
disabled by default today (`tick_engine.py:53,112-114`), so enabling authoritative session
statistics is a prerequisite for session-OHLC bootstrap to have any effect.

**Ingestion restart:** M1 allocates a **new epoch**; Dhan reconnect required; the backend consumer
sees a new producer incarnation; L1 ends the old incarnation and starts a new one. The backend
must treat a new `(producer_id, epoch)` as a new incarnation (fresh continuity), never carry the
prior incarnation's terminal state forward.

**Shutdown:** M2 drains under a bounded timeout, surfacing `DrainResult(drained_complete,
pending_at_stop)`; L1 records clean vs incomplete drain.

## Redis contracts

- **Stream retention / dedup horizon (B4):** the invariant is **`dedup_ttl_seconds` ≥ maximum
  legitimate redelivery horizon ≥ oldest retained stream entry's age**. Stream retention is
  `MAXLEN`-bounded (count, not time), so its time-horizon depends on event rate; the default
  `maxlen=100_000` may be too small at peak tick rates for a full session and must be
  **calibrated during shadow** (peak events/s × max tolerated backend outage, with margin).
  Default `dedup_ttl_seconds=86_400` (1 day) covers a single intraday session with restart slack;
  raise it if retention is intended to span multiple days. Do **not** activate authoritative mode
  with a known TTL/retention/outage mismatch.
- **Pending recovery (B3):** the C1 consumer already reclaims stale PEL entries via bounded paged
  `XAUTOCLAIM` (`claim_idle_ms=30_000`), one page/cycle; C1 suppresses reclaimed-but-applied
  entries. A restarted backend never strands old PEL entries.
- **Consumer identity (B):** `consumer_group="backend"` (group-wide); `consumer_name` per backend
  instance. C1 dedup keys (`md:dedup:…`) are **group/application-wide**, independent of
  `consumer_name` — dedup scope survives consumer-name changes.
- **Reference bootstrap:** ingestion is the sole reference writer; backend is the reader; on
  restart the backend loads current-day reference state, and treats absent state as warming-up
  (fail-closed, not empty-authoritative). D1 ordering (epoch-major/sequence-minor, non-destructive
  price merge) is preserved.

## Redis failure model

| Failure | Ingestion | Backend | L1 state | Restart | Operator |
|---|---|---|---|---|---|
| Redis down to ingestion | M2 worker publish fails → boundary `FAILED` (fail-closed); submissions rejected | consumer read-fails, counted, degraded | BROKEN (worker/publication) | ingestion restart re-establishes | investigate Redis |
| Redis down to backend | unaffected (keeps publishing to stream) | consumer read-fails, **not ready** | HEALTHY (producer side) | backend resumes on Redis recovery | none if transient |
| Redis restart (AOF `everysec`) | reconnect + resume | reconnect + pending recovery; **may miss the ~1 s AOF tail of post-D1-confirmed events** | HEALTHY after reconnect (producer-side blind to tail loss — B11) | automatic | verify AOF; rely on consume-side loss detector |
| Redis total data loss | ingestion restart allocates a new epoch **only if its own durable epoch-file volume is intact** (the epoch file lives on the ingestion host volume, **not** Redis; ADR-020 volume-loss caveat applies only to loss of *that* file) | stream+dedup+reference all gone → warming-up, fail-closed | new incarnation | full re-warm | restore/accept re-warm |

Fail closed wherever continuity cannot be proven; a stale backend never reports ready. Because
producer-side L1 confirms delivery at D1's XADD ack and cannot observe a subsequent Redis AOF
tail-loss, the shadow/authoritative **consume side must run a canonical-sequence gap/loss detector**
(per producer incarnation) so post-confirmation loss is caught rather than silently accepted (B11).
That detector must **reconcile a consumer-observed gap against the producer's L1 record** — a
sequence the producer marked *published* but the consumer never received is a real loss, whereas a
sequence the producer marked *overflow/rejected* is a legal gap (§L1 legal-gap semantics) and must
not be flagged. Raw consumer-side sequence arithmetic alone cannot distinguish the two.

## Safety

- **Single Dhan owner (B6):** steady-state (Mode 3+) is guaranteed by ingestion `replicas=1` +
  backend `market_provider_enabled=false` (backend constructs no `DhanRestAdapter`). Accidental
  double-start of the ingestion service is prevented by deployment singleton (`replicas=1` /
  host-level service ownership); a Redis lease/fence is **not** introduced unless a real
  orchestration gap demands it. **Open item:** the Mode 1–2 shadow window has *two* concurrent
  Dhan owners (legacy backend + ingestion) on the same `dhan_client_id` — see B6 resolution.
- **Dual-authority prevention:** during shadow, only the legacy path mutates TickEngine authority;
  the IPC path is observational (shadow sink). Cutover switches authority in one controlled
  one-way step; there is never a window where both independently mutate the same TickEngine state
  (`authoritative=T ∧ legacy=T` is an illegal, fail-fast combination).
- **Fail-closed authority gate:** IPC-authoritative readiness requires C1 healthy ∧ L1 healthy ∧
  consumer healthy ∧ backlog within threshold ∧ authoritative sink ready; any failure removes
  readiness (never silently serve stale authority).

## Shadow validation

**Bounded metrics** (no per-symbol cardinality): `legacy_events_total`, `ipc_events_total`,
`canonical_match_total`, `canonical_mismatch_total`, `ipc_lag_ms`, `backlog_depth`,
`duplicate_suppressed_total`, `pending_entries`, `continuity_state`.

**Latency timestamps** (existing canonical fields only — **no FIX-2 changes**): provider
`event_timestamp`, envelope `produced_at`, consumer receive `now()`, sink apply `now()`.

**Pass criteria (go/no-go, explicit — no "looks good"):** zero unexplained canonical event loss
**as measured by the consume-side loss detector reconciled against producer L1** (not raw
arithmetic); zero unresolved L1 continuity break; duplicate suppression proven (C1 counters);
bounded lag within a calibrated threshold; clean provider reconnect observed; backend restart
catch-up proven with the H6 warm-start inaccuracies (a)/(b)/(c) understood and accepted; ingestion
restart + new-epoch handling proven (H7); reference-state parity (previous_close, session OHLC)
between paths.

## Cutover and rollback

**Cutover (reversible until H10):** 1) ingestion healthy; 2) shadow consumer healthy; 3) backlog
≤ threshold; 4) L1 healthy; 5) C1 healthy; 6) authoritative sink ready; 7) freeze/stop legacy
authority input; 8) enable `ipc_authoritative`; 9) verify first N events; 10) disable backend Dhan
ownership (`market_provider_enabled=false`).

**Rollback per stage:** shadow publisher/consumer problems → disable that flag (no authority
impact). C1/backlog/L1 problems in authoritative mode → readiness fails closed; revert
`ipc_authoritative` and re-enable legacy (**requires backend Dhan reconnect → fresh token
generation; the deploy-token hazard and 2-min generation rate limit apply**). Cutover value
mismatch → revert to legacy before H10. Rollback must never create dual authority (the switch is
one-way at any instant).

**Final legacy removal (H10)** is last and only after authoritative IPC passes defined live
validation; the backend Dhan fallback is not removed before then.

## Deployment topology

Services: `apexscan-market-ingestion` (new), `apexscan-backend`, `redis`, `postgres`, reverse
proxy. Today there is one image (`backend/Dockerfile`) and no split; the two services share that
image differing by entrypoint/CMD, or a second Dockerfile is added.

- **market-ingestion:** restart `unless-stopped`, `replicas=1`, `depends_on: redis`; **no public
  HTTP port** (loopback health probe only); mounts `/etc/apexscan/dhan.env` + `apexscan-infra.env`.
- **backend:** loopback `127.0.0.1:8000`, `depends_on: redis+postgres`; mounts `backend.env` +
  `apexscan-infra.env`, and **not** `dhan.env` after Mode 3.
- `MarketIpcConfig` is currently isolated from `Settings`/env (`market_ipc/config.py:1-6`); Phase H
  must surface the stream/group/consumer/retention/dedup fields through configuration for both
  processes (H1).

## Secret & configuration ownership

Dhan credentials belong only to market-ingestion after cutover (clean seam: `dhan.env` is already
separate). The backend requires Dhan credentials **only** when `market_provider_enabled=true`
(`settings.py:319-345`), so with legacy off the backend needs no Dhan secrets — no duplication.
Secrets are never printed. Ingestion config vs backend config vs shared Redis key/contract
settings are kept separate; neither process reads the other's irrelevant secrets.

## Blocker status

| ID | Item | Status |
|---|---|---|
| **B1** | authoritative sink duplicate safety | **DESIGN_RESOLVED** — market values idempotent (replace/max/min/delta); C1 dedups before apply |
| **B2** | apply→mark crash window | **DESIGN_RESOLVED_IMPLEMENTATION_PENDING** — value-convergent; benign ordinal churn; optional H8 hardening (reference gate / atomic apply+mark) |
| **B3** | pending-entry recovery | **DESIGN_RESOLVED** — bounded paged XAUTOCLAIM exists; verify in H6 |
| **B4** | C1 TTL vs stream retention | **DESIGN_RESOLVED_IMPLEMENTATION_PENDING** — invariant defined; calibrate MAXLEN+TTL during shadow before authoritative |
| **B5** | backend restart / bootstrap | **DESIGN_RESOLVED** — reference load + stream resume, no Dhan reconnect; catch-up limits calibrated in H6 |
| **B6** | ingestion singleton / split-brain | **DESIGN_RESOLVED_IMPLEMENTATION_PENDING** — steady-state via replicas=1 + legacy off; **shadow-window dual-Dhan needs operator decision** (second `dhan_client_id`, or confirmed Dhan concurrent-session allowance, or staging/replay shadow) |
| **B7** | health / readiness gates | **DESIGN_RESOLVED** — per-service liveness/readiness + fail-closed authority gate defined |
| **B8** | cutover rollback | **DESIGN_RESOLVED** — one-way authority switch, reversible before H10; legacy rollback token implications documented |
| **B9** | FIX-track dependency | **DESIGN_RESOLVED** — IPC reuses the same TickEngine acceptance; authoritative activation gated on FIX-track status (RC3 confirmed / FIX-2 as needed); not solved here |
| **B10** | Dhan secret migration | **DESIGN_RESOLVED_IMPLEMENTATION_PENDING** — clean `dhan.env` seam; backend needs no Dhan creds when legacy off |
| **B11** | Redis durability / fsync + post-confirmation loss detection | **DESIGN_RESOLVED_IMPLEMENTATION_PENDING** — pin `appendfsync`/RDB in a `redis.conf`; add a consume-side loss detector reconciled against producer L1 (producer-side L1 is blind to AOF tail-loss); resolve before authoritative activation |

No item is **BLOCKED**; every core architecture problem is resolvable on paper. B2, B4, B6, B10,
B11 carry an implementation-pending qualifier and must be closed at the subphase noted below before
the corresponding production-activating step.

## Recommended Phase-H subphase sequence (frozen)

`H0` design/readiness gate (this ADR) → `H1` inert production compose/service definitions + surface
`MarketIpcConfig` via configuration → `H2` market-ingestion boots with publisher **disabled** →
`H3` shadow publisher enablement — **precondition: B6 shadow-window operator decision resolved**
(second `dhan_client_id`, confirmed Dhan concurrent-session allowance, or staging/replay shadow),
because H3 is where a second live Dhan session on one client_id first appears and could disrupt the
still-authoritative legacy feed → `H4` backend shadow consumer + `CompositeDeduplicator` +
consume-side loss detector (B11) → `H5` live shadow evidence collection → `H6` backend restart /
catch-up proof (verify warm-start inaccuracies a/b/c) → `H7` ingestion restart / new-epoch proof →
`H8` authoritative-sink readiness resolution (B2/B4/B11 decisions; shared dedup namespace; reference
gate / atomic apply+mark; **verify bus subscribers — strategies/scanner/sector — tolerate a
duplicate `MarketContextUpdated` version-bump with identical values**) → `H9` controlled IPC
authority cutover → `H10` backend Dhan ownership removal.

Every subphase defines preconditions, actions, success criteria, rollback, and stop conditions; no
automatic progression; **every production-activating subphase requires explicit human approval**.
Authority (H9) is gated behind B2/B4/B11 resolution (H8), so authority is never activated before
those blockers are implementation-closed; H3 is gated behind B6.

## Non-goals / boundaries

This design does not enable IPC, activate M2/C1, add a second authoritative path, dual-run, cut
over, start any subphase, touch FIX-2 / timestamp / TickEngine acceptance, or contact production.
M1/D1/M2/C1/L1 semantics are unchanged. Status remains **Proposed** pending review; not marked
Accepted during the design phase.
