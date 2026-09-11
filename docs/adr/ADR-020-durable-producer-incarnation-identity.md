# ADR-020 — Durable Producer Incarnation Identity for Market IPC

| Field | Value |
|-------|-------|
| **Status** | Proposed |
| **Date** | 2026-09-11 |
| **Deciders** | Platform / Market-Ingestion Decoupling Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | Phase A (IPC envelope/contracts), Phase B (publisher/epoch), Phase C (consumer/dedup), Phase D (compacted reference); DECOUPLING-M1 |

---

## Context

Canonical market IPC events carry the dedup identity
`(producer_id, producer_epoch, producer_sequence)` (Phase A/B). `producer_sequence` resets to
zero on every producer restart, so `producer_epoch` **must** change on each restart and must
never be reused for a given `producer_id` — otherwise a post-restart `seq=1` collides with the
previous run's `seq=1` and a consumer would wrongly dedup a genuinely new event (Phase C).

Phase B allocated the epoch with a Redis `INCR` on `md:producer:epoch:<producer_id>`. That is
restart- and concurrency-safe **only while Redis state survives**. It is the carried blocker
**M1**: a Redis `FLUSHDB`, an append-only/RDB volume loss, or a fresh Redis instance resets the
counter to zero, so the next `allocate` returns `1` and reuses an epoch that already labelled
previously-emitted events — a silent identity collision precisely when Redis is the one thing
that was lost.

## Problem

Make producer-epoch allocation collision-safe under the supported failure model — in
particular **independent of Redis durability** — or fail closed where it cannot be guaranteed.
Preserve the existing `int` epoch contract and the `(producer_id, epoch, sequence)` schema.

## Alternatives considered

1. **Keep Redis `INCR`, mandate AOF+`fsync` + durable volume.** Rejected as the sole authority:
   it still collides on `FLUSHDB` / volume loss / new instance, and couples event-identity
   safety to Redis operational discipline that M1 cannot enforce.
2. **Random/UUID-derived epoch.** Rejected: the envelope epoch is a non-negative `int`; a random
   int gives only probabilistic (not guaranteed) uniqueness and loses the monotonic-incarnation
   semantics consumers reason about.
3. **Producer-local durable monotonic counter (chosen).** The producer owns its own incarnation
   counter in a crash-safe local file on its own durable volume; Redis is removed from the epoch
   path entirely.

## Decision

Introduce `DurableEpochAllocator` (`app/market_ipc/epoch.py`), replacing `RedisEpochAllocator`
as the epoch authority. Per `allocate(producer_id)`:

1. Validate `producer_id` against `^[A-Za-z0-9._-]{1,128}$` (fail closed on anything else — no
   path traversal into the state directory).
2. Take an exclusive `flock` on `<state_dir>/producer-epoch-<producer_id>.lock` so accidental
   concurrent starts sharing a `producer_id` are serialized and each gets a distinct epoch.
3. Read the last persisted epoch from `<state_dir>/producer-epoch-<producer_id>.json`
   (`0` if the file is missing = first-ever start). A present-but-unreadable/garbled/schema- or
   producer-mismatched/negative/non-int value raises `EpochStateError` — **never** a silent
   reset to a reusable low epoch.
4. Persist `epoch = current + 1` **before returning it**, crash-safely: write a temp file,
   `fsync` it, `os.replace` (atomic on POSIX), then `fsync` the directory.
5. Return the new epoch.

Because the value is persisted before use, a crash can only **skip** an epoch (a harmless gap),
never reuse one. The epoch path uses no wall clock, so clock rollback/NTP correction/duplicate
start-times cannot affect it. The `EpochAllocator` Protocol, the envelope schema (`int` epoch,
`schema_version=1`), and `MarketEventPublisher` are unchanged — this is an allocator swap at
composition time, and IPC remains off by default (nothing is wired into production).

## Redis durability & failure model

Redis is no longer the epoch authority; the durability burden is the producer's own local
volume. `md:producer:epoch:*` keys are legacy and unused (kept only as a documented constant).

| Failure | Expected M1 behavior |
|---|---|
| Process restart (state dir intact) | New, higher epoch; no reuse. Sequence resets under the new epoch. |
| Container restart (durable volume mounted) | New, higher epoch; no reuse. |
| Host restart (persistent volume intact) | New, higher epoch; no reuse. |
| **Redis process restart / `FLUSHDB` / RDB/AOF volume loss / fresh Redis** | **No effect on epoch** — Redis is not the authority. No collision. (This is the M1 fix.) |
| Crash between allocations (returned epoch discarded) | Next start skips it (gap); never reuses. |
| Producer epoch-state file **corrupt / partial write** (file present) | `EpochStateError`, fail closed — no silent reuse. |
| Concurrent duplicate producer start (same `producer_id`) | Serialized by `flock`; each gets a distinct epoch. |
| **Producer state-file / volume loss** (file *missing*) | **Silently restarts at epoch 1** — a missing file is indistinguishable from a genuine first start using local state alone. This is an **operational non-guarantee**, not code-enforced fail-closed: to avoid reuse after volume loss, use a fresh `producer_id` or seed a higher epoch on the new volume. |
| Total host loss | Same as volume loss: a replacement host needs the surviving durable volume, a fresh `producer_id`, or a seeded higher epoch. |

**Guaranteed (supported failure model):** no two allocations return the same epoch for one
`producer_id` across process/container/host restart **with the durable volume intact**,
crash-between-allocations, concurrent starts, and any Redis state loss.
**Fail-closed:** corrupt/partial *existing* epoch state raises `EpochStateError`.
**Operational non-guarantee (NOT code-enforced):** loss of the producer's own durable volume
silently restarts at epoch 1, because local state cannot distinguish first-start from
volume-loss; a future phase could add an explicit init/seed contract to fail closed on this.
`producer_id` reuse across a lost volume is an operational responsibility.

## Explicit non-guarantees

M1 establishes durable producer **event identity** only. It does **not** provide exactly-once
delivery or processing, nor distributed transactions. The intended end model remains
at-least-once transport + stable identities + idempotent/deduplicated consumers & state.

## Consequences

- **D1 (stream ↔ reference atomicity)** — unchanged, still `PRE-CUTOVER BLOCKER`. M1 does not
  combine XADD + reference-hash writes.
- **C1 (cross-process dedup)** — still `NOT_GUARANTEED`; M1 supplies the collision-safe identity
  C1 will rely on.
- **M2 (async publish boundary)** — unchanged/separate; epoch allocation is startup-level and
  off the per-event hot path (sequence allocation stays O(1)).
- **L1 (FeedContinuity wiring)** — untouched.
- **Operational:** the decoupled producer requires a durable local state directory (mode `0700`,
  files `0600`, no secrets) that survives restarts; back it with the same persistent volume
  lifecycle as the producer. The concurrent-start guarantee relies on `flock`, which is reliable
  on a **local POSIX volume**; do not place the state directory on an NFS mount where `flock`
  semantics are not guaranteed.
