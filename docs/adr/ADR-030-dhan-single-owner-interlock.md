# ADR-030 — Cross-process Dhan single-owner interlock (Redis lease + fencing)

| Field | Value |
|-------|-------|
| **Status** | Accepted (ratifies ADR-027's open interlock decision as **Option I2**; implemented offline in H9A, not activated) |
| **Date** | 2026-09-15 |
| **Deciders** | Platform / Market-Ingestion Decoupling Architecture (ratified by repository owner at H9A) |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | ADR-026 (H3 shadow-publish / no-lease stance — this ADR reverses it), ADR-027 (single-Dhan-owner migration policy; interlock `ARCHITECTURE_DECISION_REQUIRED`), ADR-025 (authority cutover), ADR-029 (B11 loss detection); Phase H9A |

> Ratifies the interlock mechanism ADR-027 §103 left open. It is implemented **offline** in H9A
> (isolated Redis + fake providers) and **activates nothing**: no production, no real Dhan, no
> ownership transfer (that is H9B). It reverses ADR-026's "no Redis lease/fencing" stance, so it is
> its own accepted decision.

## Context

`SINGLE_DHAN_OWNER = TRUE` (ADR-027): at no instant may the backend and market-ingestion both own
the real Dhan session. The existing controls are insufficient to *hard-guarantee* cross-process XOR:
`Settings.validate_single_dhan_owner` is single-process only (cannot see two containers); Compose
profile-gating is operational convention, not a runtime fail-fast. ADR-027 recorded the requirement
and deferred the mechanism to two options — **I1** (operational sequencing + startup owner-marker
check) and **I2** (Redis ownership lease + fencing token) — recommending I2.

## Decision

Adopt **I2**. A broker-neutral ownership coordinator (`app/market_ingestion/ownership.py`) gates Dhan
ownership on an exclusive, TTL-bounded Redis lease carrying a monotonic **fencing generation**.

**Ownership record** — one Redis key `md:provider:ownership` holding
`{owner_role, instance_id, fencing_generation, acquired_at_ms}` with `EX = lease_ttl_seconds`.
`owner_role ∈ {BACKEND, INGESTION}`; `instance_id` is a per-**incarnation** id (not a fixed
container name). The fencing generation is a monotonic counter (`md:provider:ownership:fence`,
`INCR`) advanced on every fresh acquisition.

**Operations (all atomic via a single server-side Lua call; fail closed on any Redis error):**
- `acquire(role, instance_id)` — if no live owner: `INCR` the fence, write the record with TTL,
  return the lease. If the *same* `(role, instance_id)` already owns: idempotent — refresh the TTL,
  keep the generation. If a *different* owner holds a live lease: fail (return no lease).
- `renew(lease)` — refresh the TTL **only** if the current record matches the lease's
  `(role, instance_id, generation)` exactly; else fail (a stale/superseded holder cannot renew).
- `release(lease)` — delete the record **only** if it matches `(role, instance_id, generation)`;
  never an unconditional `DEL` (a stale holder cannot delete a newer owner's lease).
- `validate(lease)` — true iff the current record matches `(role, instance_id, generation)`.

**Fencing guarantee.** A takeover after expiry mints a strictly higher generation, so a paused old
holder (gen N) fails `renew`/`release`/`validate` against the new holder (gen N+1). Even if a Redis
reset cleared the fence counter (B11/H8C detects and fails closed), a stale holder still fails
because the record is absent or carries a different `(role, instance_id)`.

**Ownership precedes the provider.** A process MUST hold a valid lease before any Dhan
auth/connect/subscribe: `acquire → validate → provider connect`. On lease loss (renew failure, TTL
expiry, fencing mismatch, Redis unavailable) the provider is treated as no longer authorized and is
stopped/disconnected. Inability to *prove* ownership is *no permission to own* — never fail open.

**Lease vs. lock.** A bounded TTL (`0 < renewal_interval < lease_ttl`, validated) with periodic
owner-scoped renewal, so a crashed owner's lease expires without manual cleanup; no eternal lock.

## Why not I1

I1 (operational sequencing + a shared owner marker) keeps single-ownership as operational discipline
plus a soft startup check; a mis-written/mis-cleared marker or config drift can still yield two
owners. I2 makes `NEVER_TWO_LIVE_DHAN_OWNERS` a **runtime invariant** enforced by atomic Redis
semantics + fencing — required for authority readiness (ADR-025/H9C). I1 was offered only as an
explicit interim; I2 is adopted directly.

## Consequences

- Reverses ADR-026's no-lease stance for the Dhan-owner seam specifically (streams/dedup unchanged).
- H9A implements + proves this **offline** (unit + real-Redis race/fencing/fail-closed + offline
  cutover/rollback rehearsal). It does not deploy the interlock, contact Dhan, or transfer ownership.
- **Preconditions for use at H9B** (not in H9A): production deployment of the interlock in both
  services, the governed cutover/rollback runbook, the Dhan token-lifecycle plan, and FIX-2 for live
  correctness. `READY_FOR_H9B` stays gated on those.
