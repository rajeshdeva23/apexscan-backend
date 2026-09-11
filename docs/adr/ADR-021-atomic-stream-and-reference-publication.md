# ADR-021 — Atomic Canonical Event and Compacted Reference Publication

| Field | Value |
|-------|-------|
| **Status** | Proposed |
| **Date** | 2026-09-11 |
| **Deciders** | Platform / Market-Ingestion Decoupling Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | Phase A (envelope/transport), Phase B (publisher), Phase D (compacted reference), ADR-020 (durable producer epoch); DECOUPLING-D1 |

---

## Context

A reference-bearing market event needs two Redis effects: a canonical append to the event
stream (`XADD md:events`) and a transition of the compacted reference projection
(`md:reference:<trading_date>`, keyed by instrument identity — `previous_close` from
`MarketReference`, session OHLC from `Tick.session_ohlc`). Phase B appended the stream and
Phase D compacted the reference as **two separate round-trips**.

## Problem (D1)

Two separate operations leave a partial-write window:

- reference `HSET` succeeds, then a crash/failure before `XADD` → the recovery projection holds
  state for an event that is absent from canonical stream history;
- `XADD` succeeds, then a crash/failure before the reference update → the stream holds a
  canonical event whose restart-recovery projection is missing.

D1 requires: for a STREAM_PLUS_REFERENCE event the stream append **and** the reference
transition either **both** become visible or **neither** does; no other client observes one
half without the other.

## Decision

Perform both effects in a **single server-side Lua script** (`_PUBLISH_STREAM_AND_REFERENCE_LUA`,
`app/market_ipc/atomic.py`), `KEYS=[stream, reference_hash]`:

1. Parse/validate all numeric arguments (maxlen, epoch, sequence, ttl) and `cjson.decode` the
   existing + incoming state **before the first write**; on failure return `redis.error_reply`
   (no effect performed).
2. Classify the reference transition against the stored ordering (Phase-D semantics, unchanged):
   `stale_rejected` / `duplicate` (no-op) or `written` / `merged` (non-destructive price merge).
3. `XADD` the canonical event (**always** — a real event is never dropped because its compacted
   projection is a no-op).
4. When the transition is written/merged, `HSET` the reference and refresh its TTL.

A **STREAM_ONLY** event (no reference data) uses a plain `XADD` and never touches a reference key
or its TTL. The publisher classifies each event via `reference_from_envelope` and routes
accordingly.

### Alternatives considered
- **`MULTI`/`EXEC`** — queued commands can't branch on the read (ordering/merge decision) within
  the transaction; would need `WATCH`+retry, which retry-exhausts under contention (the same
  reason Phase D chose Lua for the reference alone). Rejected.
- **Application-level write-A-then-undo-B** — non-atomic, leaves the very window D1 removes.
  Rejected.
- **Distributed lock around the two writes** — adds a lock-liveness failure mode and is not an
  atomicity primitive. Rejected.

## Critical Lua caveat

Redis Lua is atomic with respect to other clients, but Redis does **not** database-rollback
writes a script already performed if it errors later. Therefore every fallible check runs
**before the first write**; after `XADD` the script issues only deterministic, already-validated
commands. This is asserted by a fault-injection test (malformed epoch → `error_reply` → neither
stream nor reference mutated). We do **not** claim "Lua rolls back failed scripts."

## Producer ordering, retry, TTL, topology

- **Ordering** consumes M1 identity unchanged: monotonic `(producer_epoch, producer_sequence)`;
  a higher epoch (sequence reset to 1) still progresses; a lower epoch never regresses.
- **Retry / unknown outcome:** if the client loses the connection after Redis committed, it
  cannot know the result; a retry may append **another** stream record with the same canonical
  identity. That does not violate D1 atomicity — stream de-duplication is **C1**, not D1.
- **TTL:** refreshed only on write/merge (Phase-D behavior); a stale/duplicate no-op and a
  STREAM_ONLY append do not touch reference TTL.
- **Topology:** the script touches two keys on one node → **single-instance Redis**. Redis
  Cluster cross-slot behavior is not claimed/supported here.

## Explicit non-guarantees

- **No exactly-once** delivery or processing; no exactly-once stream append under uncertain
  retry. Target model stays at-least-once transport + stable identity (M1) + atomic reference
  projection (D1) + durable/idempotent consumers (C1).
- **Cross-process dedup = NOT_GUARANTEED** (C1).
- D1 does **not** move Redis work off the ingestion hot path (that is M2).

## Consequences

- **M2** (async publish boundary) — unblocked; the atomic call is one synchronous round-trip.
- **C1** (cross-process dedup) — still NOT_GUARANTEED; consumes the atomic, ordered identity.
- **L1** (FeedContinuity wiring) — untouched.
- **Phase H** — not started; nothing is composed or enabled (IPC remains off by default).
