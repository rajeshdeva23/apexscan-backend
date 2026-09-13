# ADR-026 — H3 Shadow-Publish Topology and Dhan-Ownership Strategy (B6)

| Field | Value |
|-------|-------|
| **Status** | Proposed (DESIGN REVIEW — no implementation, no activation) |
| **Date** | 2026-09-13 |
| **Deciders** | Platform / Market-Ingestion Decoupling Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Amended by** | ADR-027 (§B6 live-shadow sub-decision & recommended subphases — single-owner migration policy; the live dual-owner shadow / H3E is superseded) |
| **Related** | ADR-020 (M1), ADR-021 (D1), ADR-022 (M2), ADR-023 (C1), ADR-024 (L1), ADR-025 (Phase-H design; B6); DECOUPLING H1/H2 |

> **Design review only.** No code, no production contact, no real Dhan, no Redis publication, no
> M1/M2/D1/L1 activation. This ADR freezes the H3 shadow-publish architecture and resolves the B6
> Dhan-ownership question for the live-shadow window before any H3 implementation begins.

---

## Context

The decoupling foundation and the standalone ingestion service are complete on
`feature/decoupling-hardening` (`547a501`): M1/D1/M2/C1/L1 merged; H1 added the inert composition +
Phase-H flag matrix; H2 gave the ingestion service a real provider lifecycle (Dhan
auth/provider/WebSocket ownership) with IPC **off**. H3 will wire the publication path
`Dhan → ingestion service → M1 → M2 → D1 → Redis` while the backend legacy path stays
authoritative and the IPC consumer stays off.

The blocking question is **B6**: live side-by-side shadow needs the legacy backend **and** the
ingestion service both connected to Dhan → **two live Dhan owners on one `dhan_client_id`**.

### Verified current state (`547a501`)
- **Ingestion service owns** (H2): `DhanRestAdapter` (provider), `DhanAuthManager` (auth,
  instance-scoped lazy-cached token), `WebsocketsDhanLiveTransport` (WS), subscription lifecycle,
  via `ProviderCoordinator` + `ProviderSupervisor` → `ProviderOnlyEventSink` (counting only).
- **Backend legacy provider** is gated by `market_provider_enabled` and composed in
  `dhan_runtime_composition.compose_market_runtime`.
- **A fan-out-before-authority seam already exists** in `market_runtime.py`: the
  `market_event_publisher` parameter + `_publish_shadow` tee each canonical datum to the IPC
  publisher **after** authoritative dispatch — off by default, **not** composed in production.
- **Singleton guards today**: `Settings.validate_single_dhan_owner` (rejects
  `market_provider_enabled ∧ market_ingestion_service_enabled` in one process — shared-config
  defence-in-depth only) + the `market-ingestion` Compose profile (excluded from the default stack).
- `M2.submit` is synchronous/non-blocking; `M2.start()` calls `publisher.start()` which allocates
  the M1 epoch — so M1 allocation is naturally tied to publisher activation.

## B6 decision

**`B6_DECISION = NO_LIVE_H3_YET`.**

H3 is implemented and proven against a **fake/replay provider + a real test Redis**; **no live
Dhan shadow** runs until a later, separately-approved live sub-decision.

### Rationale
- The safety principle (ADR-025 §Safety, B6) is **one live Dhan owner**. Live
  side-by-side shadow inherently needs two owners during the H3–H5 window.
- There is **no documented or verified evidence** that Dhan permits two concurrent live-feed
  sessions for a single `dhan_client_id`, and assuming provider limits is forbidden. Recorded
  hazard: Dhan rate-limits token **generation** to ~once/2 min and the token cache is
  instance-scoped; many broker APIs also invalidate the prior session on a new login. A second
  Dhan session could therefore **disrupt the authoritative production feed** — unacceptable for a
  shadow test.
- Governance rule (fail-closed when evidence is insufficient): if a safe live topology cannot be
  chosen on the available evidence, defer live activation and continue with fakes only. The
  evidence bar is not met for a safe live dual-Dhan or fan-out topology today, so live is deferred.

This maximises confidence in the **decoupled architecture** (the standalone ingestion service's
real `provider → M1 → M2 → D1 → Redis` path is exercised end-to-end against real Redis, minus only
the live Dhan socket) with **zero** dual-Dhan risk and **zero** production contact.

### Options evaluated
| Option | Verdict |
|---|---|
| **A — Controlled dual Dhan** | **Rejected now.** Safe only with a provisioned *second* `dhan_client_id`/account (external, unprovisioned) or documented concurrent-session support (unverified). Otherwise risks the authoritative feed. |
| **B — Single Dhan owner, backend fan-out** | **Not selected.** The existing `market_event_publisher` seam is safe (one owner) but co-locates the publisher in the **backend** and does **not** exercise the standalone ingestion service (it validates the publication path, not the service split). Recorded as a fallback for validating D1/M2 co-located only; still requires approval and produces live Redis traffic. |
| **C — Ingestion owns Dhan + temp legacy bridge** | **Rejected.** Prematurely moves authority-critical Dhan ownership to an unproven service and reintroduces a bridge equivalent to early cutover; high complexity/risk. |
| **D — Non-production validation (chosen)** | **Selected as `NO_LIVE_H3_YET`.** Full H3 pipeline validated with fake provider + real test Redis; live shadow deferred. |

### Deferred live-shadow sub-decision (EXTERNAL VERIFICATION REQUIRED)
Live shadow (H3E) is gated on establishing **one** of:
1. a provisioned **second Dhan `client_id`/account** for the ingestion service → clean
   `CONTROLLED_DUAL_DHAN` with independent sessions/tokens; or
2. **documented/verified** Dhan concurrent-session support for one `client_id`; or
3. a **maintenance-window one-way governed swap** (skip live side-by-side; validate via
   replay + fast rollback).

None is verified today; each is flagged external-verification-required.

## Frozen H3 architecture (to implement, not now activated)

### Startup order (fail-closed; Dhan connects last)
`validate config → Redis reachable → construct D1 publisher → M2.start()` (allocates the **M1
epoch** via `publisher.start()`) `→ L1 producer_started(incarnation) → construct provider → Dhan
connect → subscribe → READY`. The safety-critical invariant is that **the live WebSocket session
and token generation start only after `M2.start()`** — i.e. the live feed (which lazily generates
the Dhan token and opens the WS at `stream_market_data` subscribe) must never begin before the
publication infrastructure is ready, so canonical events always have a safe destination. (The
benign `provider.connect()` — HTTP clients + instrument-master REST, token still lazy — may run
during composition; it is the live WS/token session that must be gated after M2.)

### M1 startup contract
The M1 epoch is allocated **only when `ipc_publisher_enabled=true`** (as a side effect of
`M2.start()`→`publisher.start()`), never in provider-only mode. H2's no-epoch-in-provider-only
guarantee is preserved.

### Event-publication contract (D1 semantics unchanged)
`Tick`/`Quote` → `STREAM_ONLY`; `MarketReference` (previous_close) and `Tick.session_ohlc` →
`STREAM_PLUS_REFERENCE` (atomic append + reference compaction via the existing D1 routing through
`reference_from_envelope`). No D1 semantic change.

### Publication sink
Replace `ProviderOnlyEventSink` with a `PublishingEventSink` whose synchronous `handle(datum)`
calls `M2.submit(datum)` (O(1), non-blocking) and feeds the result to L1 + the failure policy.
This fits the existing sync `sink.handle` contract in `ProviderSupervisor`.

### L1 integration (H3 first activates L1)
- `producer_started(producer_id, epoch)` at epoch allocation.
- `record_submission(SubmitOutcome, producer_sequence)` at each `M2.submit`.
- Completion + worker-fault via **`observe_boundary(boundary.diagnostics())`** on a **bounded
  cadence (frozen: a small configurable interval, default ≤ 1 s)** — ADR-024 makes
  `observe_boundary` **mandatory** for worker-fault detection; H3 drives it (M2 semantics stay
  frozen; no observer hook is added inside M2). The cadence bound caps the detect→disconnect window;
  it is combined with the sink's synchronous terminal-outcome raise (above) so an overflow is
  actuated immediately rather than only at the next poll.
- `provider_connected` / `provider_disconnected` from the provider lifecycle.
- One boundary per producer incarnation (ADR-024 seen-counter baseline requirement).

### Shutdown order
`stop provider intake → close subscription/WS → M2 bounded drain (DrainResult) → L1 final state →
process stop`. No new provider events after the drain begins.

### Fail-closed actuation seam (terminal-vs-recoverable contract)

The H2 `ProviderSupervisor` self-heals **every** non-cancel stream exception with bounded
reconnect. That is correct for a provider-transport disconnect but **wrong** for a publication
continuity break — reconnecting into a dead/overflowed publisher would loop or silently drop
canonical events (`REJECTED_NOT_RUNNING`, which L1 ignores). H3 therefore introduces an explicit
**terminal-vs-recoverable** contract (new ingestion-service-layer wiring; **no M2 change**):

- **Recoverable** = a provider-transport failure (WS disconnect). The supervisor self-heals as
  today (bounded reconnect, same epoch); L1 → `PROVIDER_DEGRADED` → `HEALTHY` on next publish.
- **Terminal (publication continuity break)** = `REJECTED_OVERFLOW`, M2 worker `FAILED`, terminal
  D1 publish failure, or a terminal Redis-publish outage. On a terminal break the service **stops
  provider intake, disconnects the provider, and marks the service `FAILED`/NOT_READY**; the
  supervisor **must not** reconnect a publication-terminal break.
- **Actuation path.** The `PublishingEventSink.handle` maps each `M2.submit` outcome to L1
  (`record_submission`) and, on a terminal submit outcome (`REJECTED_OVERFLOW` /
  `REJECTED_NOT_RUNNING`, the latter meaning the worker already faulted), raises a dedicated
  `PublicationTerminalError`. The service treats that (and an L1 `BROKEN` state observed via the
  boundary poll below) as fail-closed: cancel the supervisor, `provider.disconnect()`, set
  `FAILED`. The supervisor must add a **dedicated `except PublicationTerminalError: raise` handler
  ahead of its generic recoverable-reconnect catch-all** so the terminal break propagates and stops
  intake instead of being self-healed into a reconnect loop (`PublicationTerminalError` is still an
  `Exception`, so the earlier handler is required — the type alone does not bypass the catch-all).
  This actuation seam is the core deliverable proven with fakes in H3B.

## Failure policies (all fail-closed → disconnect provider)

- **Queue overflow (`REJECTED_OVERFLOW`)** — terminal continuity break for the incarnation: stop
  provider intake, disconnect the provider, mark the service `FAILED`/NOT_READY. **Never** continue
  publishing an incomplete canonical stream; never silently drop.
- **M2 worker terminal failure (`BoundaryState.FAILED`)** — L1 `BROKEN` → disconnect provider →
  NOT_READY. No silent worker resurrection.
- **D1 publication failure** — surfaced via the boundary's publish-failure counter into L1; a
  terminal condition fails closed as above.
- **Redis unavailable before provider start** — the provider **must not connect** (startup fails
  closed).
- **Redis fails terminally mid-session** — L1 unhealthy → NOT_READY → disconnect provider (do not
  keep receiving events that cannot be safely published).
- **Provider disconnect** — L1 `PROVIDER_DEGRADED` (recoverable); reconnect keeps the **same**
  producer_epoch (same auth owner, cached token — no regeneration); L1 returns to `HEALTHY` only
  after a subsequent successful publication (recovery evidence).

## Configuration

- **H3 test config** (fake provider + real test Redis): `market_ingestion_service_enabled=true`,
  `ipc_publisher_enabled=true`, `ipc_consumer_enabled=false`, `ipc_shadow_compare_enabled=false`,
  `ipc_authoritative_enabled=false`, `legacy_market_path_enabled=true`. Derives
  `INGESTION_SHADOW_PUBLISH` — **representable in the ADR-025 matrix, no new mode, no deviation**.
  In the ingestion process `market_provider_enabled=false` (single-Dhan-owner guard satisfied).
- **H3 live config** — identical flags with a real provider, **gated on the deferred B6 live
  sub-decision** (`NO_LIVE_H3_YET`). Not permitted now.
- **Technical live-activation interlock (not procedural only).** A deployed ingestion process always
  builds a **real** `DhanRestAdapter` (the fake provider is test-injection only), so
  `ipc_publisher_enabled=true` in a deployed process ⇒ live Dhan ⇒ dual ownership. H3 therefore adds
  a fail-fast Settings guard: **`ipc_publisher_enabled=true` requires `live_h3_publish_approved=true`**
  (a new flag defaulting `false`). Until the B6 live sub-decision is made and that approval flag is
  set, an accidental publisher-on deployment **refuses to boot** (same fail-closed style as the
  existing validators). Tests inject the service directly and bypass this guard, so H3A/H3B remain
  fully exercisable with fakes. Frozen here; implemented in H3C.
- **Illegal combinations** unchanged (ADR-025 / H1 matrix), plus the new
  `ipc_publisher_enabled ⇒ live_h3_publish_approved` guard above.
- **Consumer/shadow/authoritative stay OFF** (H4 owns shadow consume).

## Singleton mechanism

Single-host Compose: the single-instance guarantee comes from a **single service definition + a
daemon-global unique `container_name` (`apexscan-market-ingestion`) + manual profile activation** —
this blocks a second instance and `docker compose up --scale market-ingestion=N` on the host. (Note:
a `deploy.replicas` directive is **swarm-only** and is a no-op for plain `docker compose up`, so it
is deliberately not relied upon.) Combined with `validate_single_dhan_owner` (shared-process) this
prevents an accidental second **local** Dhan owner. **No Redis lease/fencing is added** (no
distributed leadership — YAGNI for single-host Compose). A future multi-host/orchestrated deployment
would require a Redis lease/fence; that is explicitly deferred.

## Stream growth / retention

H3 (when live) creates real Redis stream traffic. Monitor stream length, publish rate,
oldest-entry age, and Redis memory. `MAXLEN` stays `100_000` (B4 calibration remains pending — do
not change retention until shadow evidence justifies it). Producer-side positions (sequence,
last-published) + L1 continuity are exposed; the **consume-side loss detector is H4/H8** (B11 not
resolved by H3).

## Recommended execution subphases

`H3A` publication composition (`PublishingEventSink` + M1/M2/D1/L1 wiring) proven with a **fake
provider + real test Redis** → `H3B` failure-path integration tests (overflow, Redis outage,
worker fault, reconnect, restart/new-epoch) → `H3C` Compose/config readiness (default stack still
excludes ingestion; publisher-on config frozen) → `H3D` live-activation readiness review (the
deferred B6 live sub-decision) → `H3E` live shadow publisher (**separate explicit approval only**).
Code readiness and live activation are never combined.

### Live H3 go/no-go (all required)
H3 code tests green · B6 live sub-decision made (second credential / verified concurrent sessions /
governed swap) · Docker/Compose validation complete · Redis persistence policy understood (B11) ·
M2/L1 fail-closed proven · FIX-track status acceptable (B9) · explicit human approval.

## Blocker status after H3 design

| ID | Status |
|---|---|
| B2 (apply→mark) | DESIGN_RESOLVED_IMPLEMENTATION_PENDING |
| B4 (retention/TTL) | DESIGN_RESOLVED_IMPLEMENTATION_PENDING |
| **B6 (Dhan ownership)** | **DESIGN_RESOLVED_IMPLEMENTATION_PENDING** — explicit `NO_LIVE_H3_YET`; live sub-decision deferred with external verification required |
| B10 (secret migration) | DESIGN_RESOLVED_IMPLEMENTATION_PENDING |
| B11 (durability / loss detector) | DESIGN_RESOLVED_IMPLEMENTATION_PENDING (consume-side detector = H4/H8) |

## Scope / non-goals

No IPC/M1/M2/D1/L1 activation, no Redis publication, no real Dhan, no consumer/shadow/cutover, no
TickEngine/authority/FIX-2/timestamp change, no production contact, no merge to `main`. M1/D1/M2/
C1/L1 and ADR-025 semantics unchanged. Status remains **Proposed** pending review.
