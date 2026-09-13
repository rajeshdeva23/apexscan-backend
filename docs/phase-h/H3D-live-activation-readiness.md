# PHASE H3D — Live shadow-publish activation readiness review (B6 gate)

**Status:** readiness review only — **no real Dhan activation performed**. Delivered via PR into the
`feature/decoupling-hardening` holiday branch (NOT `main`). Governed by ADR-025 / ADR-026.

**Result:** `PHASE_H3D_PASS` · `B6_DECISION = NO_LIVE_H3_YET` · `READY_FOR_H3E = NO`.

This phase determines whether ApexScan can safely proceed to **H3E** (governed live shadow-publisher
activation) without disrupting the existing authoritative production market feed. It produces an
evidence-backed GO/NO-GO. It does **not** perform activation, connect real Dhan, generate a token,
open a WebSocket, deploy, or change production.

## Baseline

- Production `main` = `a6b8c68ddd87e5d2a00c19485a2bf116285641fe` — unchanged, not contacted.
- Holiday `feature/decoupling-hardening` = `477cadf780bc7abf3c602da3c57e017ebb7ac11d`.
- Completed: M1/D1/M2/C1/L1, Phase-H design, H1, H2, H3 design, H3A, H3B, H3C.
- Production contacted? **NO** — the full topology was established from the repository (production
  Compose, composition code, Dhan auth lifecycle); read-only host inspection was unnecessary.
- Framing remediation `2deddf1` confirmed in the holiday ancestry (§24); framing not redesigned.

## Current topology (repository evidence)

| Fact | Value | Source |
|------|-------|--------|
| `CURRENT_LEGACY_DHAN_OWNER` | the `apexscan-backend` process (`market_provider_enabled=true` → `dhan_runtime_composition` builds the Dhan adapter + `DhanAuthManager` + live WS) | VERIFIED_REPOSITORY |
| `FUTURE_INGESTION_DHAN_OWNER` | `apexscan-market-ingestion` (only when `market_ingestion_service_enabled=true` **and** `live_h3_publish_approved=true`) | VERIFIED_REPOSITORY |
| `CURRENT_SECRET_OWNER` | production `dhan.env` mounted by **both** `backend` and `market-ingestion` (`docker-compose.production.yml:71,115`) | VERIFIED_REPOSITORY |
| `INGESTION_SECRET_ACCESS` | YES — mounts `/etc/apexscan/dhan.env` (but the service is profile-gated + inert today) | VERIFIED_REPOSITORY |
| `LIVE_H3_INTERLOCK` | `Settings.validate_h3_live_publish_interlock` — `ipc_publisher_enabled ⇒ live_h3_publish_approved` (default `false`), fail-fast | VERIFIED_REPOSITORY |
| `SINGLETON_CONTROLS` | `validate_single_dhan_owner` (shared-process: not both `market_provider_enabled ∧ market_ingestion_service_enabled`) + unique `container_name: apexscan-market-ingestion` + profile gating (not default-started) + no public port | VERIFIED_REPOSITORY |

**Secret topology (§13):** `BACKEND_HAS_DHAN_SECRET_ACCESS = YES`,
`INGESTION_HAS_DHAN_SECRET_ACCESS = YES`, `SAME_IDENTITY_POSSIBLE = YES` (same `dhan.env` ⇒ same
`client_id`). The same credential material is therefore mountable in two containers — this *enables*
the B6 hazard and is controlled only by keeping ingestion inert/unapproved.

## Desired H3E topology (frozen for reference; NOT approved)

Producer-shadow only: `Dhan → market-ingestion → M1 → M2 → D1 → Redis`, with the backend legacy
`Dhan → TickEngine` path remaining authoritative and the **IPC consumer OFF**. This requires the
legacy backend **and** the ingestion service to be **simultaneous** Dhan owners (§16–§17).

## Token lifecycle (§11)

| Fact | Value | Source |
|------|-------|--------|
| `TOKEN_GENERATION_OWNER` | `DhanAuthManager` per provider/process (TOTP → `auth.dhan.co/app/generateAccessToken`) | VERIFIED_REPOSITORY |
| `TOKEN_CACHE_LOCATION` | **process memory only** (`_RuntimeAccessToken`; never written to disk/Redis) | VERIFIED_REPOSITORY |
| `TOKEN_PERSISTENCE` | NONE | VERIFIED_REPOSITORY |
| `RESTART_BEHAVIOR` | a fresh process has no cached token → regenerates on first use (container recreation forces regen — see [[apexscan-dhan-token-deploy-hazard]]) | VERIFIED_REPOSITORY |
| `SECOND_PROCESS_BEHAVIOR` | a second same-`client_id` process runs its **own** `DhanAuthManager` and generates its **own** token independently | VERIFIED_REPOSITORY |
| token validity | 24 hours | VERIFIED_OFFICIAL |
| `TOKEN_REGENERATION_COOLDOWN` | **2 minutes** — Dhan answers a too-soon request with HTTP 200 + `"Token can be generated once every 2 minutes."`, mapped to a rate-limit error in `auth.py` | VERIFIED_REPOSITORY (provider runtime message; not restated as a number on the fetched official pages) |

## External provider evidence (§9)

Official DhanHQ v2 documentation, accessed 2026-09-13:

| Claim | Finding | Classification | Source |
|-------|---------|----------------|--------|
| simultaneous market-feed WS connections | "up to five WebSocket connections per user with 5000 instruments on each connection"; a 6th disconnects the 1st with `805` | VERIFIED_OFFICIAL | Live Market Feed (dhanhq.co/docs/v2/live-market-feed) |
| instruments per connection | up to 5000 | VERIFIED_OFFICIAL | same |
| instruments per subscription message | up to 100 | VERIFIED_OFFICIAL | same |
| WS authenticated by access token | yes — `token=` query parameter | VERIFIED_OFFICIAL | same |
| token validity | 24 hours | VERIFIED_OFFICIAL | Authentication (dhanhq.co/docs/v2/authentication) |
| one active token per `client_id`? | **not stated** | UNKNOWN | Authentication |
| new token invalidates previous? | **not stated** | UNKNOWN | Authentication |
| two processes, same `client_id`, concurrent valid tokens? | **not stated** | UNKNOWN | Authentication |
| same-`client_id` concurrent **feed** support (two independent auth sessions) | the WS limit is "per user"; the docs do **not** address two independent same-`client_id` authenticated feed sessions, and token coexistence is unstated | **UNKNOWN** | Live Market Feed + Authentication |

**Three distinct limits are not conflated (§28, §38 Q2/Q14):** (a) ≤5 WebSocket *connections* per
user; (b) ≤5000 *instruments* per connection; (c) ≤100 instruments per *subscription message*.
"Multiple subscriptions supported" refers to (c) on one WS, not to (a) concurrent authenticated
sessions.

**Decisive gap:** the WS *connection count* (5/user) would numerically permit backend + ingestion (2
of 5), **but** each ApexScan process authenticates independently and Dhan's official docs do **not**
state whether a second same-`client_id` token generation invalidates the first, nor whether two
same-`client_id` tokens coexist. Per §10/§34, this is **UNKNOWN** and must not be read as YES.

`SAME_CLIENT_CONCURRENT_FEED_SUPPORT = UNKNOWN`.

## B6 analysis — options (§8)

| Option | Safety | Op. complexity | Token risk | Authoritative-feed risk | Rollback | Evidence required | Recommended |
|--------|--------|----------------|------------|-------------------------|----------|-------------------|-------------|
| **A — separate shadow identity** (distinct `client_id`/account for ingestion) | **highest** (no shared token/session space) | medium (provision a 2nd Dhan account with the same market-data entitlements + universe) | none | none | trivial (stop ingestion) | provision + entitlement confirmation | **YES — path to clear B6** (not provisioned today) |
| **B — same identity concurrency** | **UNKNOWN** | low | **HIGH** (2nd generation may invalidate the authoritative token; 2-min cooldown → throttled thrash) | **HIGH** | medium/uncertain (backend token may already be invalid; regen throttled) | official proof of concurrent same-`client_id` token/session coexistence — **NOT FOUND** | **NO — cannot clear B6 (§34)** |
| **C — governed single-owner swap** (stop legacy → ingestion sole owner → collect evidence) | disruptive | high (maintenance window, ordered stop/start) | low/medium (one owner at a time; swap-back regenerates backend token under the 2-min cooldown) | **medium — authoritative feed intentionally goes dark; this is NOT side-by-side shadow** | stop ingestion → restart backend (token regen, cooldown gap) | explicit operational acceptance of an authoritative-feed maintenance window; validates the **producer pipeline only**, not concurrent shadow | conditional fallback, separate authorization required |
| **D — shared single token** (one generator, both processes reuse) | n/a today | — | — | — | — | not the current architecture; would require new token-sharing code (out of H3D scope) | none viable now |

**Recommended:** **Option A (separate identity)** is the cleanest route to a true side-by-side
H3E shadow with zero authoritative-feed risk. It is not provisioned today, so B6 cannot be cleared
now. Option C remains a narrower fallback for producer-pipeline evidence only, at the cost of an
authoritative-feed maintenance window, and needs its own explicit authorization.

## B6 decision (§35)

`B6_DECISION = NO_LIVE_H3_YET` — unchanged. No official evidence establishes same-`client_id`
concurrent feed/token safety, and no separate shadow identity is provisioned. This is the correct
conservative outcome (§44), not a failure of the review.

## Safety posture (§14/§15)

- Live interlock **proven**: `ipc_publisher_enabled=true` without `live_h3_publish_approved=true`
  fails Settings construction (`test_settings_reject_publisher_without_live_approval`); with approval
  it derives `INGESTION_SHADOW_PUBLISH` (`test_settings_allow_publisher_with_live_approval`). Default
  `live_h3_publish_approved=false`.
- Ingestion profile-gated, not default-started, singleton `container_name`, no public port
  (`docker-compose.production.yml` + `tests/deploy/test_compose_dev.py`).
- `docker compose --profile market-ingestion up` with `MARKET_INGESTION_SERVICE_ENABLED=true` **and**
  `LIVE_H3_PUBLISH_APPROVED=true` would make both backend and ingestion Dhan owners — this is the
  governed, interlocked path, never the default; the default `up` never starts ingestion.
- Consumer/C1 remain OFF (flags default off; `validate_settings` rejects consumer-without-role; not
  composed). Dual authority not possible without deliberately flipping approved flags.

## H3E design (frozen for a future authorized activation only)

- **Topology:** producer-shadow only (above); backend authoritative; consumer OFF; contingent on B6
  clearance (Option A) — **not approved now**.
- **Universe (§27):** first activation should use a **small governed validation subset**, not the
  full universe — bounded subscription (≤100/message, far under 5000/connection), higher evidence
  quality, lower provider load — then expand in later stages.
- **Provider mode (§29):** unchanged (RequestCode 17 / quote mode); no full-feed switch.
- **Staged duration (§26):** Stage 1 short controlled window → Stage 2 longer observation → Stage 3
  full-session shadow. Not scheduled or executed here.
- **Evidence plan (§25):** provider (connect / reconnect / subscription / instruments / canonical
  events); M1 (producer_id / epoch / sequence monotonicity); M2 (accepted_total / queue depth / high
  watermark / overflow_total); D1 (published_total / stream-length delta / reference publication);
  L1 (provider state / accepted position / published position / continuity state / terminal reason);
  safety (legacy provider health unchanged / no authority change / consumer OFF).
- **Stop conditions (§22):** legacy feed disconnect · unexpected Dhan auth/session error ·
  unexpected token regeneration · ingestion reconnect storm · L1 terminal break · M2 overflow ·
  D1/Redis terminal publication failure · universe subscription mismatch · framing failure ·
  provider timestamp/clock anomaly affecting producer evidence.
- **Rollback (§21):** stop ingestion → confirm no new token/session side effects → legacy remains /
  returns sole owner. Clean for Option A (independent token space). For same-identity it is
  **not** clean (a regenerated token may already have invalidated the authoritative token, and
  re-generation is throttled 2 min) — another reason Option B is not approved.

## Dependencies

- **FIX-2 (§23):** `H3E_FIX2_DEPENDENCY = INDEPENDENT_FOR_PRODUCER_SHADOW`. Producer-shadow pipeline
  evidence (connect/subscribe/decode/publish/identity/ordering/continuity) does not depend on Dhan
  timestamp semantics. FIX-2 is **required before** any consume-side comparison (H4) or any
  timestamp-correctness claim; H3E producer evidence must not be read as validating timestamps.
- **B10 secret migration (§30):** required **before** H9/H10 authority cutover, **not** before H3E.
  Temporary dual `dhan.env` visibility is acceptable for shadow validation (the backend keeps its own
  Dhan access until cutover); do not confuse it with final single-ownership. Do not migrate now.
- **B2 / B4 / B11 (§31):** not producer-shadow prerequisites (consumer OFF). No H3E dependency found.

## Adversarial review (§38) — summary

15 questions asked; no assumption of two-session safety without evidence (Q1: UNKNOWN → NO_LIVE_H3);
"multiple subscriptions" correctly separated from concurrent WS sessions (Q2/Q14); token
generation possibly invalidating the authoritative session (Q3/Q4) and restart-triggered 2-min
rate-limit (Q5) are the **HIGH live-safety risks** — they correctly force `READY_FOR_H3E = NO`
rather than flawing the review; dual authority / accidental consumer / accidental Compose activation
(Q6/Q7/Q8) are blocked by defaults + interlock + single-owner + profile gating; rollback-without-
new-auth is clean only for Option A (Q9); we explicitly do **not** rely on undocumented behavior
(Q10 — UNKNOWN kept UNKNOWN); FIX-2 blocks consume-comparison not producer evidence (Q11); same
credential in two containers is a real structural fact controlled by non-activation (Q12); official
limits interpreted correctly (Q13); H3E cannot affect trading/strategies (Q15). **No unresolved HIGH
in the review's own conduct; all identified HIGH live-safety risks are contained by
`NO_LIVE_H3_YET` / `READY_FOR_H3E = NO`.**

## GO / NO-GO

**NO-GO for H3E.** The review is complete and correct; the evidence is insufficient to clear B6 for
the same-identity concurrent-shadow topology, and no separate identity is provisioned. To reach a GO:
provision a **separate shadow Dhan identity** (Option A) with matching market-data entitlements and
universe, then re-run this gate to conclude `CLEARED_FOR_H3E_SEPARATE_IDENTITY` — after which H3E
still requires a separate explicit authorization (§36).

## Blocker status

- **B6** — `NO_LIVE_H3_YET` (DESIGN_RESOLVED_IMPLEMENTATION_PENDING; live sub-decision needs external
  verification or a separate identity).
- **B2 / B4 / B10 / B11** — DESIGN_RESOLVED_IMPLEMENTATION_PENDING; none is an H3E producer-shadow
  prerequisite (B10 gates H9/H10 cutover).
- **B1 / B3 / B5 / B7 / B8 / B9** — RESOLVED (per ADR-026).

## Explicitly NOT done in H3D

No real Dhan auth/token/WS, no live shadow publish, no ingestion activation, no production
deploy/restart/mutation, no IPC consumer/C1, no H4, no authority cutover, no strategy/trading change,
no FIX-2, no timestamp change, no credential rotation, no secret migration, no production contact, no
new ownership architecture frozen (Option A recommendation would warrant an ADR-026 addendum **if**
later chosen). `READY_FOR_H3E = NO`, `READY_FOR_H4 = NO`, `READY_FOR_IPC_AUTHORITY = NO`.
