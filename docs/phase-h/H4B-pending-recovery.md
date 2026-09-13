# Phase-H4B — Redis Stream Pending-Entry Recovery (Offline / Test-Redis Only)

Governed by **ADR-023** (durable consumer idempotency) and **ADR-025** (activation flag matrix).
H4B proves that entries stranded in the Redis consumer-group PEL (Pending Entries List) after a
consumer failure are reclaimed and reprocessed through the **same** C1 correctness gate, without
violating the H4A apply→mark→ACK and durable-idempotency invariants. No new architecture, no new
config, no ADR.

> **H4B DOES NOT USE REAL DHAN. H4B DOES NOT CONTACT PRODUCTION. H4B DOES NOT ACTIVATE THE
> CONSUMER IN PRODUCTION. H4B DOES NOT MAKE IPC AUTHORITATIVE. H4B DOES NOT SOLVE B2 / B4 / B11.
> H4B DOES NOT IMPLEMENT FIX-2 OR TOUCH FIX-2A PR #63. H4B DOES NOT START H4C.**

## Why PEL recovery is required

Redis Streams deliver at-least-once. An entry delivered by `XREADGROUP` enters the group PEL and
stays there until `XACK`. If a consumer crashes / is terminated / loses its ACK after delivery, the
entry is stranded under that consumer forever unless another consumer reclaims it. H4B reclaims such
entries by idle age and reprocesses them safely.

## Recovery mechanism (already present, now proven + observable)

The H4A consumer's `poll_once` already performs a **bounded `XAUTOCLAIM` reclaim pass, then a new
read pass**, and both passes route each entry through the same `_handle` → gate → C1 dedup → decode
→ sink → mark → ACK path. H4B adds no second recovery path; it adds recovery-scoped observability
and exhaustive proof.

```
poll cycle:
  XAUTOCLAIM (min_idle = claim_idle_ms, start "0-0", COUNT = read_count)   ← recovery pass
      → each reclaimed entry → process_stream_entry (shared C1 gate)
  XREADGROUP (">", COUNT = read_count)                                     ← new pass
      → each new entry → process_stream_entry (shared C1 gate)
```

- **Redis command** — `XAUTOCLAIM` (via `RedisMarketEventStream.claim_page_raw`; tolerates the 6.2
  2-tuple and ≥7.0 3-tuple responses).
- **Idle threshold** — `MarketIpcConfig.claim_idle_ms` (the repository-equivalent of
  `pending_claim_min_idle_ms`; default 30 000 ms, validated 1 000–600 000 ms). Tests use a tiny
  window via `model_copy` (production calibration is out of scope).
- **Recovery batch size** — `read_count` (default 100, validated 1–10 000); one bounded page per
  cycle, so no unbounded single-cycle claim.
- **Cursor** — each recovery pass starts from `"0-0"` and claims one bounded page; the returned
  next-cursor is intentionally not carried, because `XAUTOCLAIM` resets the idle clock of the
  entries it returns, so the next cycle's `"0-0"` scan skips just-claimed entries and advances to
  the next still-idle page. A backlog larger than one page therefore drains across successive
  cycles, and there is no infinite cursor loop.

## Recovery scheduling (no starvation)

One recovery pass then one new pass per cycle. Recovery can never starve new traffic (it claims at
most one bounded page per cycle) and new traffic can never strand pending work (a recovery pass runs
every cycle, including the first cycle after startup). Recovery-on-startup falls out of this: the
first `poll_once` reclaims before the process has read any new entries.

## Shared C1 processing path

Reclaimed and newly-read entries are indistinguishable to `_handle`. The C1 order is unchanged:

- **New (durable `contains` = false)** → sink apply → durable mark → `XACK`.
- **Duplicate (durable `contains` = true)** → no reapply → `XACK`.

## Recovery cases (all tested against real redislite)

| Case | State before recovery | Recovery outcome |
|------|----------------------|------------------|
| **ACK-lost** | applied + durably marked, ACK failed → pending | durable `contains` = true → **no reapply** → ACK |
| **Apply-failed** | sink failed, no mark, no ACK → pending | `contains` = false → **retry sink** → mark → ACK |
| **Mark-failed (B2)** | applied, durable mark failed, no ACK → pending | `contains` may be false → **sink can reapply** (duplicate application) |
| **Abandoned consumer** | delivered to a dead consumer, never ACKed | another consumer `XAUTOCLAIM`s after idle → processes safely |
| **Below idle** | pending under a (nominally live) owner | not stolen until `claim_idle_ms` elapses |

### ACK-lost safety vs the apply→mark B2 limitation

The ACK-lost case is safe because the durable mark committed *before* the ACK was lost, so recovery
sees the durable duplicate and suppresses reapply. The **mark-failed** case is the known **B2**
window: the sink applied but the durable mark never committed, so a reclaim finds no durable record
and reapplies — the shadow sink sees the event twice. H4B **demonstrates and counts** this
(`test_apply_then_mark_crash_reclaim_reapplies_b2_window` asserts the apply count reaches 2); it
does **not** hide or claim to eliminate it. Coupling the durable mark atomically with an
authoritative sink transition is **B2**, owned by H8A. It is harmless here only because the sink is
non-authoritative.

## Failure handling (fail-closed)

No reclaimed entry is reported safely completed unless its correctness conditions actually held:

- **Claim (`XAUTOCLAIM`) failure** — counted (`pending_reclaim_failures` + `read_failures`); the
  cycle ends; no false ACK; the loop backs off (no busy loop).
- **Dedup-store failure during recovery** — fail-closed (`dedup_store_failures`); entry left pending.
- **Sink failure during recovery** — not ACKed (`sink_failures`); entry stays pending and retries.
- **Durable-mark failure** — not ACKed (`dedup_store_failures`); B2 window (above).
- **ACK failure** — counted (`ack_failures`); entry stays pending and redelivers.

## Poison-message policy boundary

`POISON_RETRY_POLICY = UNRESOLVED_LATER_PHASE`. H4B invents no DLQ / max-delivery / destructive
ACK. The H4A distinction is preserved exactly:

- **Permanently invalid / undecodable poison** → visible (counter) + **terminal ACK** so it cannot
  jam the group (tested on reclaim).
- **Processable but transiently failing** → never terminally discarded; left pending / retryable.

## Consumer identity model

Recovery is **idle-based** (`XAUTOCLAIM min_idle`), so it reclaims abandoned entries whether the
original owner was the same configured consumer name after a restart or a different dead consumer
name — both are proven. A unique-per-incarnation naming scheme is therefore not required for
correct idle-based recovery and is not introduced here.

## Ordering guarantee

H4B makes **no** global exactly-in-order guarantee across newly-delivered and reclaimed entries:
reclaim order (PEL idle scan) and new-delivery order (stream order) differ. Producer sequence
numbers are **never** used to reorder or to infer loss. Legal producer sequence gaps (a sequence
allocated before enqueue, then dropped on bounded-queue overflow) and out-of-order sequences are
tolerated; a new producer epoch with a repeated sequence is a distinct identity, never a duplicate.

## Shutdown

Once the runtime is STOPPING it starts no new recovery work and claims no further batches; a
reclaimed entry that is mid-processing at shutdown is **not** ACKed and stays pending for a later
incarnation to reclaim (tested). The blocked read + recovery loop cancels promptly and the owned
Redis client is closed exactly once (H4A lifecycle, unchanged).

## Observability

`ConsumerDiagnostics` gains bounded scalar counters (no per-entry cardinality):
`pending_recovery_runs`, `pending_reclaimed`, `pending_reclaim_failures`,
`pending_reclaimed_applied`, `pending_reclaimed_duplicates`. Live pending depth is read on demand
with `XPENDING` in tests/ops — deliberately not part of the cheap in-memory snapshot.

## Blocker statuses (unchanged)

- **B2** (apply→mark atomicity) = `DESIGN_RESOLVED_IMPLEMENTATION_PENDING` — H8A. Made more
  observable here, not solved.
- **B4** (stream retention vs dedup/redelivery horizon) = `DESIGN_RESOLVED_IMPLEMENTATION_PENDING` —
  H8B. H4B may reveal how long entries can stay pending; it claims no TTL/retention safety.
- **B11** (Redis durability / stream-loss detection) = `DESIGN_RESOLVED_IMPLEMENTATION_PENDING` —
  H8C. PEL recovery is not durability proof and infers no stream loss.

## H4C boundary

H4C owns the shadow comparison / replay framework and parity between expected canonical events and
the consumed path (timestamp-aware only once FIX-2 prerequisites permit). H4B implements no
comparison logic. FIX-2A remains INCONCLUSIVE and untouched; H4B uses fixture timestamps only and
makes no live-timestamp claim.
