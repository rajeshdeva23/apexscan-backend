# ADR-027 — Single-Dhan-Owner Migration Policy

| Field | Value |
|-------|-------|
| **Status** | Proposed (DESIGN / ROADMAP AMENDMENT — no implementation, no activation, no real Dhan) |
| **Date** | 2026-09-13 |
| **Deciders** | Platform / Market-Ingestion Decoupling Architecture |
| **Amends** | ADR-025 (rollout/migration sequence), ADR-026 (§B6 live-shadow sub-decision & recommended subphases) |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | ADR-020 (M1), ADR-021 (D1), ADR-022 (M2), ADR-023 (C1), ADR-024 (L1); Phase-H H3D readiness review |

> **Design / roadmap amendment only.** No code, no production contact, no real Dhan auth/token/WS,
> no Redis publication, no consumer/authority activation, no FIX-2. This ADR amends the migration
> *strategy* after the H3D finding; it does not change the destination architecture and implements
> nothing.

---

## Context

ADR-025 froze the destination two-service topology (`market-ingestion → Redis → backend`) and a
rollout that passes through live side-by-side shadow modes (`INGESTION_SHADOW_PUBLISH`,
`SHADOW_CONSUME_COMPARE`). ADR-026 deferred the live-shadow window as `B6_DECISION = NO_LIVE_H3_YET`,
expecting it to later clear via **Option A (a second Dhan account)** or **Option B (documented
concurrent same-`client_id` sessions)**.

**H3D (readiness review) established:**

- `SAME_CLIENT_CONCURRENT_FEED_SUPPORT = UNKNOWN` — official DhanHQ docs do not state whether a new
  same-`client_id` access token invalidates the prior one, nor whether two same-`client_id`
  authenticated feed sessions safely coexist (token generation is throttled ~2 min; the token is
  process-memory-only). Enabling the ingestion service **at all** makes it a live Dhan owner (token +
  WebSocket), so a live side-by-side shadow means **two live Dhan owners on one `client_id`** — the
  exact arrangement whose safety is unverified.
- H3D **recommended Option A** (a *separate* shadow Dhan identity) as the cleanest, highest-safety
  route to a true side-by-side shadow, but noted it is **not provisioned or approved today**.

**This amendment decides** not to make provisioning a second Dhan identity a migration
*prerequisite*: with same-`client_id` concurrency UNKNOWN and no second identity available now, the
roadmap must **not depend on simultaneous Dhan ownership**. (This is ADR-027's decision, not an H3D
finding; if a second identity is later provisioned, H3D's Option A remains the recommended path and
this policy can be revisited.) Both ADR-026's dual-owner live-shadow window (H3E) and ADR-025's live
side-by-side shadow-compare modes are therefore off the critical path.

## Decision

**`SINGLE_DHAN_OWNER = TRUE` — a frozen migration invariant.** At every point in the migration,
**exactly one** process owns the live Dhan connection: either the backend legacy provider **or**
`apexscan-market-ingestion`, **never both**.

Consequences of the invariant:

1. **The destination architecture is unchanged** (ADR-025 §Target ownership): `Dhan →
   market-ingestion → Redis → backend → TickEngine/MarketContext → sector/strategies/APIs`; after the
   final cutover the backend holds no Dhan auth/WS/credentials.
2. **No live dual-owner shadow.** ADR-026's `H3E` live shadow-publisher (dual owner) is
   **SUPERSEDED_BY_SINGLE_OWNER_CUTOVER** — not `PASS`, not deleted; its design/evidence and the H3D
   `UNKNOWN` finding are preserved.
3. **The IPC path is validated OFFLINE**, not by live side-by-side comparison. Consumer/C1 wiring,
   failure/recovery, and shadow-compare are proven with isolated/test Redis and replay/synthetic/
   recorded fixtures (H4A–D, H5). ADR-025's `INGESTION_SHADOW_PUBLISH`/`SHADOW_CONSUME_COMPARE` modes
   remain representable flag states but are **not** exercised as *live* dual-owner windows.
4. **The first live Dhan activation of ingestion IS the governed single-owner cutover** (H9B): the
   backend Dhan provider is stopped and verified closed *before* ingestion connects. This creates a
   **finite feed gap** that must be measured, bounded, and (where broker historical APIs permit)
   caught up — see the roadmap's gap analysis. The gap is a deliberate, governed cost of never having
   two owners.
5. **A hard single-owner interlock is required** (see Open decisions).

## Revised phase map (amends ADR-026 §"Recommended execution subphases")

```
H3A ✅  H3B ✅  H3C ✅  H3D ✅
H3E   = SUPERSEDED_BY_SINGLE_OWNER_CUTOVER   (no live dual-owner shadow)

FIX-2 track  (separate; required before consume-compare / timestamp-correctness claims — H3D §23)

H4A = consumer composition + C1 wiring over isolated/test Redis            (no live Dhan)
H4B = consumer pending-recovery / XAUTOCLAIM / failure hardening           (no live Dhan)
H4C = shadow-compare framework over replay / recorded / synthetic fixtures (no live Dhan)
H4D = consumer readiness review

H5  = offline/replay parity + cutover evidence preparation
H6  = backend restart behavior under the IPC test topology (no Dhan reconnect)
H7  = ingestion restart / new-epoch behavior under the test topology

H8A = B2 authoritative-sink apply→dedup crash-window closure
H8B = B4 stream retention vs dedup/redelivery horizon contract
H8C = B11 Redis durability + consume-side loss-detection contract
H8D = authority readiness review

H9A = single-owner cutover preparation (incl. the single-owner interlock)
H9B = governed live ownership transfer (backend Dhan stop → verify closed → ingestion sole owner)
H9C = authority verification + rollback gate (IPC becomes backend authority)

H10 = remove backend Dhan ownership/secrets (B10 completion)
```

Labels may be refined by later phases, but **no risk gate may be silently collapsed**. `H8A/B/C`
(B2/B4/B11) and a proven single-owner interlock are **mandatory before IPC authority** (H9C).

## Single-owner interlock — ARCHITECTURE_DECISION_REQUIRED (open)

The existing controls are **insufficient** to *hard-guarantee* cross-process XOR:

- `Settings.validate_single_dhan_owner` rejects `market_provider_enabled ∧ market_ingestion_service_enabled`
  **within one process only** — it cannot see the two-container topology, so two separate containers
  can each be configured as a Dhan owner (config drift).
- Compose profile-gating + the default `MARKET_INGESTION_SERVICE_ENABLED=false` keep ingestion inert
  by default, but are operational conventions, not a runtime fail-fast.

ADR-026 deliberately added **no Redis lease/fencing** ("YAGNI for single-host Compose"). The
single-owner migration revives that question. A **hard, fail-fast, cross-process single-owner
interlock is required** before H9B. The **mechanism is an open architecture decision** to be ratified
(via acceptance of this ADR / at H9A) — this ADR records the requirement but implements nothing:

- **Option I1 — Operational sequencing + startup cross-check (lighter).** The governed cutover
  procedure (stop backend Dhan → verify closed → start ingestion) is the primary control; add a
  startup-time check that reads a shared owner marker and refuses to connect if another owner is
  active. Still depends on a shared marker being written/cleared correctly.
- **Option I2 — Redis ownership lease / fencing token (stronger, fail-fast).** Each process must
  acquire an exclusive Dhan-owner lease (single-holder, TTL + fencing token) before any Dhan connect;
  a second owner cannot acquire it and fails closed. This is a genuinely new architecture-level
  mechanism (reverses ADR-026's no-lease stance) and must be its own accepted decision.

**Recommendation:** adopt **I2** (or I1 as an explicit interim) so `NEVER_TWO_LIVE_DHAN_OWNERS`
becomes a runtime invariant, not merely operational discipline. Until the interlock is implemented,
the invariant is guaranteed **only** by the governed cutover sequence + config discipline — this ADR
does not implement it and flags it as the phase's `ARCHITECTURE_DECISION_REQUIRED`.

## Token lifecycle & rollback under single ownership

- Because only one owner exists at a time, ingestion at cutover **generates its own fresh token** (the
  token is process-memory-only and not shared across processes; cross-process token reuse is **not**
  in the current design — recorded as a cutover operational dependency, not assumed).
- **Rollback** (ingestion fails post-transfer): stop ingestion → ensure the Dhan owner is cleared →
  restore backend Dhan ownership → verify feed. Restoring the backend requires the backend to
  **regenerate a token**, subject to the ~2-min generation cooldown → a rollback carries a bounded
  re-authentication gap. A rollback that needs another token generation is **operationally risky until
  proven** and must be rehearsed (H9A) before H9B.
- **First live contact is the cutover (accepted residual risk).** Because Option A (a second identity)
  is not a prerequisite, ingestion's live Dhan auth/WS/universe-subscription cannot be rehearsed
  against real Dhan *before* H9B — the cutover is the first live ingestion Dhan contact. This is
  inherent to single-owner + no-second-account and is mitigated (not eliminated) by the abort
  conditions and a rehearsed rollback; it is accepted knowingly, and argues for running the first
  cutover outside the live session.
- **B10 secret migration is staged:** backend keeps Dhan secrets through cutover and rollback; backend
  Dhan credentials are removed **only after** a proven cutover (H10). Never remove backend credentials
  before the rollback strategy is frozen and rehearsed. **During the H9B→H10 window** the backend
  still holds Dhan secrets, so single ownership is enforced by the single-owner interlock + the
  disabled legacy path (`market_provider_enabled=false`), **not** by secret-absence — a backend
  restart in this window must not re-acquire Dhan (the backend-restart-no-Dhan criterion under
  *Success criteria preserved* below is only *structurally* guaranteed once H10 removes the secrets).

## Blocker impact

| Blocker | Status after this amendment |
|---|---|
| B1, B3, B5, B7, B8, B9 | RESOLVED (unchanged) |
| **B6** (Dhan ownership) | **RESOLVED_BY_MIGRATION_POLICY (design)** — the roadmap no longer requires concurrent same-`client_id` feeds, so `SAME_CLIENT_CONCURRENT_FEED_SUPPORT = UNKNOWN` is off the critical path. **Caveat:** the runtime `NEVER_TWO_LIVE_DHAN_OWNERS` guarantee depends on the single-owner interlock (open, above); until implemented it holds by operational sequencing only. Not claimed as a runtime-enforced guarantee yet. |
| **B2** (apply→dedup crash window) | DESIGN_RESOLVED_IMPLEMENTATION_PENDING — **mandatory before IPC authority** (H8A). |
| **B4** (retention vs dedup horizon) | DESIGN_RESOLVED_IMPLEMENTATION_PENDING — **mandatory before IPC authority** (H8B). |
| **B10** (secret migration) | DESIGN_RESOLVED_IMPLEMENTATION_PENDING — staged; completes at H10 after proven cutover. |
| **B11** (Redis durability + consume-side loss detection) | DESIGN_RESOLVED_IMPLEMENTATION_PENDING — **mandatory before IPC authority** (H8C). |

## Success criteria preserved (ADR-025)

- After final cutover, **restarting the backend must not** regenerate a Dhan token, reconnect Dhan,
  or interrupt ingestion (H6 validates this under the test topology).
- Ingestion restart allocates a new M1 epoch, reconnects Dhan, and the backend consumer continues via
  C1 redelivery/idempotency (H7).

## What this ADR does NOT do

No implementation, no real Dhan, no production contact/deploy, no consumer/authority activation, no
H4 start, no FIX-2, no interlock code, no change to the destination architecture, no edit to accepted
ADR history (ADR-025/026 are Proposed and receive only a forward "amended by" reference).
`READY_FOR_FIX2_TRACK = YES`, `READY_FOR_H4A = CONDITIONAL_ON_DEFINED_FIX2_DEPENDENCY`,
`READY_FOR_LIVE_CUTOVER = NO`, `READY_FOR_IPC_AUTHORITY = NO`.
