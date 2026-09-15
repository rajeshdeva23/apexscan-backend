# H9A — single-Dhan-owner cutover preparation (cross-process interlock)

**Phase:** DECOUPLING H9A (offline preparation — no production, no real Dhan, no cutover)
**Base:** `feature/decoupling-hardening` @ `31f6b62` (H8D PASS, READY_FOR_H9A)
**Scope:** implement and prove **offline** the cross-process single-Dhan-owner interlock (ADR-030,
Option I2 — Redis lease + fencing) and rehearse the cutover/rollback with fake providers. **Does not
perform the real ownership transfer (H9B), contact Dhan, deploy anything, or activate IPC authority.**

## Ownership topology

`SINGLE_DHAN_OWNER = TRUE` (ADR-027). Valid states: `NONE`, `BACKEND`, `INGESTION`. Invalid at any
instant: `BACKEND + INGESTION`. Cutover is `BACKEND → NONE → INGESTION`; rollback is
`INGESTION → NONE → BACKEND`. There is never — even transiently by design — a two-owner state.

The interlock decision ADR-027 left open (`ARCHITECTURE_DECISION_REQUIRED`) is ratified here as
**Option I2** in **ADR-030 (Accepted)**; ADR-027 is now **Accepted**.

## Lease + fencing contract (`app/market_ingestion/ownership.py`)

- One Redis key `md:provider:ownership` = `{owner_role, instance_id, fencing_generation,
  acquired_at_ms}` with `EX = lease_ttl_seconds`. `owner_role ∈ {BACKEND, INGESTION}`; `instance_id`
  is a per-**incarnation** id (never a fixed container name). Fencing generation = a monotonic
  `INCR` counter (`md:provider:ownership:fence`) advanced on every fresh acquisition.
- **acquire** (atomic Lua): no live owner → `INCR` fence + write record + TTL → lease; same
  `(role, instance)` → idempotent (refresh TTL, keep generation); a *different* live owner → fail
  (`None`), never steals a valid lease.
- **renew** / **release** / **validate** (atomic): succeed only if the current record matches
  `(role, instance, generation)` exactly. A stale/superseded holder is rejected. `release` never does
  an unconditional `DEL`.
- **Fail closed:** any Redis error → `acquire` returns `None`, `renew`/`validate` return `False`.
  Inability to *prove* ownership is *no permission to own* — never fail open.
- **Clock model:** correctness uses Redis server time — the lease TTL (Redis `EX`) and the acquire
  timestamp (`redis.call('TIME')`) — never drifting backend/ingestion wall clocks, and it is
  independent of the Dhan LTT / FIX-2 timestamp domain.

## Fencing semantics

A takeover after expiry mints a strictly higher generation, so a paused old holder (gen N) fails
`renew`/`release`/`validate` against the new holder (gen N+1). Verified over 1,000 acquire→release
cycles: generations are strictly increasing, contiguous under a live Redis, never reused.

## Redis failure behaviour

Acquire/renew/validate against an unavailable Redis all fail closed (no ownership granted); a
snapshot read fails closed to "no owner". A **corrupt/undecodable owner record** is treated exactly
like an unreadable one — `validate`/`snapshot` return "no owner" (never raise, never grant), and the
failed decode is counted in `redis_error_total`. A `Redis reset` (fence + key gone) is detected by
B11/H8C and, because the record is absent, a stale holder still fails `validate` — so a reset can
never resurrect stale ownership. (ADR-030 fencing-durability note.)

## Ownership precedes the provider

The enforced ordering is `acquire → validate → provider connect`; on lease loss (renew failure, TTL
expiry, fencing mismatch, Redis unavailable) the provider is treated as unauthorized and stopped.
H9A proves this with fake providers only.

## Cutover / rollback rehearsal (offline, fake providers)

`BACKEND → NONE → INGESTION` and the reverse were rehearsed with observable fake providers; the
number of concurrently-active providers **never exceeded one** (`MAX_CONCURRENT_PROVIDER_OWNERS == 1`),
the transfer always passed through a `NONE` state, and the new owner's fence strictly exceeded the
old. Failure rehearsals (new owner cannot acquire; a contender holds ownership) halt **safe** at one
owner and never auto-restart the old owner into a dual-ownership state — **safety over availability**.

## Token lifecycle & secrets (H9B prerequisites, not done here)

At the real cutover, ingestion generates its **own** fresh Dhan token; rollback requires the backend
to regenerate its token (a cooldown-bounded feed gap). H9A models **no** token generation and makes
**no** assumption of a guaranteed cooldown — recorded as an H9B operational prerequisite. Dhan
secrets are not moved (backend keeps `dhan.env` through cutover+rollback; H10 removes it).

## Boundaries

- **Dhan ownership ≠ IPC authority.** This interlock is permission to establish the *provider
  session*; it is not permission to make the Redis consumer authoritative (`ipc_authoritative`).
- **FIX-2:** the offline interlock does not depend on Dhan timestamp correctness
  (`FIX2_DEPENDENCY_FOR_H9A = NO`), but live cutover verification does
  (`FIX2_DEPENDENCY_FOR_LIVE_VERIFICATION = YES`).
- **H9B** owns the real transfer; **H9C** owns post-transfer authority verification/rollback; **H10**
  removes the backend Dhan path/secrets. H9A performs none of these.

## Test evidence

- `tests/unit/test_market_ingestion_ownership.py` — lease-timing invariant (`0 < renewal < ttl`),
  bounds, import purity.
- `tests/integration/test_market_ingestion_ownership_redis.py` (real `redislite`) — acquire/conflict/
  same-role-race, renew/release/validate + stale rejection, TTL expiry + higher-fence re-acquire,
  fail-closed on Redis unavailable, fail-closed on a corrupt/undecodable record, bounded
  diagnostics, 1,000-race contention (never two owners),
  1,000-generation fencing monotonicity, offline cutover + rollback rehearsals
  (`MAX_CONCURRENT_PROVIDER_OWNERS == 1`), and safe cutover-halt on contention.
- `tests/architecture/test_market_ipc_import_boundary.py` — ownership imports no
  TickEngine/strategy/API/Dhan surface.

## Residual gates before H9B

Real provider binding — the module enforces the lease contract but does not itself bind a Dhan
provider; `acquire → validate → connect` and `lose-lease → stop` are only rehearsed with fakes here,
so H9B must wire and prove them against the real provider lifecycle. A unique per-incarnation
`instance_id` (e.g. `uuid4`, not a fixed container name) must be enforced at composition so a
fence-reset cannot collide with a reused id. Production deployment of the interlock in both services;
the governed cutover/rollback runbook (`H9B-single-owner-cutover-runbook.md`, DRAFT — not authorized
for execution); the Dhan token plan; Redis durability policy pinned (B11 op-side); FIX-2 for live
correctness; ADR-028/029 acceptance for IPC authority. `READY_FOR_H9B` stays gated on these.
