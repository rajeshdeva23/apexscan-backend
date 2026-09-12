# ADR-022 — Bounded Asynchronous Market IPC Publication Boundary

| Field | Value |
|-------|-------|
| **Status** | Proposed |
| **Date** | 2026-09-12 |
| **Deciders** | Platform / Market-Ingestion Decoupling Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | Phase B (publisher), ADR-020 (M1 durable epoch), ADR-021 (D1 atomic publication); DECOUPLING-M2 |

---

## Context

The IPC publisher (Phase B / D1) publishes **synchronously**: the ingestion callback awaits the
Redis `XADD`/atomic-Lua round-trip. That couples provider-feed throughput to Redis latency,
stalls, and network faults — a slow Redis would back-pressure straight into the Dhan ingestion
loop.

## Problem (M2)

Move Redis I/O off the ingestion hot path while preserving M1 identity, D1 atomicity, event
ordering, bounded memory, and explicit failure — with **no silent event loss**.

## Decision

Add `AsyncPublicationBoundary` (`app/market_ipc/boundary.py`):

```
ingestion callback → submit() → bounded queue → one ordered worker → MarketEventPublisher.transmit → Redis
                     (O(1), no I/O)                                    (existing D1 path)
```

- **`submit(datum)` is synchronous, non-blocking, no Redis I/O.** It calls the publisher's new
  `prepare()` (CPU-only: allocate the producer sequence — fixing `(producer_id, producer_epoch,
  producer_sequence)` — build + size-check the envelope), then `queue.put_nowait(envelope)`.
- **Bounded queue** (`asyncio.Queue(maxsize=capacity)`, `publish_queue_capacity`, default 10 000).
  A full queue returns `REJECTED_OVERFLOW` **immediately** — never blocks, never drops/coalesces/
  overwrites silently. `submit()` never awaits.
- **One ordered worker** dequeues FIFO and calls `MarketEventPublisher.transmit(envelope)` (the
  existing STREAM_ONLY / D1 STREAM_PLUS_REFERENCE path). A single worker guarantees Redis
  publication order == submission order == `producer_sequence`.
- **Identity is fixed at submit** and carried through the queue unchanged; the worker transmits
  the pre-built envelope, so a re-`transmit` never reallocates a sequence.

The publisher was split into `prepare()` (front half, no I/O) + `transmit()` (back half, Redis);
`publish()` = `prepare` then `transmit`, unchanged for the synchronous path.

### Rejected alternatives
- **Synchronous Redis publication** — the problem itself.
- **Unbounded queue** — trades a Redis stall for unbounded memory growth / OOM.
- **`await queue.put(...)`** (block when full) — reintroduces back-pressure into ingestion.
- **Silent drop / coalesce / latest-wins on overflow** — loses canonical events; forbidden.
- **Concurrent worker pool** — can reorder the canonical producer sequence.

## Lifecycle

- **Startup:** `start()` first `await publisher.start()` (allocates the M1 epoch) — fail-closed:
  if that raises, no worker/queue accepts anything — then creates the worker and goes `RUNNING`.
  `submit()` before `RUNNING` → `REJECTED_NOT_RUNNING`.
- **Provider reconnect:** an ordinary Dhan socket reconnect does **not** recreate the boundary,
  queue, worker, or producer epoch — those are process-lifetime, not per-connection.
- **Shutdown:** `stop()` → `STOPPING` (reject new submits) → `queue.join()` under a bounded
  `publish_shutdown_drain_timeout_seconds` (default 5 s) → cancel worker → `STOPPED`. A drain
  timeout (or a previously-failed worker) is surfaced as `DrainResult(drained_complete=False,
  pending_at_stop=N)` — accepted items are never silently discarded, and shutdown never hangs.
- **Cancellation:** `CancelledError` is never swallowed; the worker keeps queue `task_done()`
  accounting consistent and terminates predictably.

## Failure semantics

- **Worker fault:** an unexpected exception in `transmit` marks the boundary `FAILED`, records a
  sanitized reason, and stops accepting submissions (fail-closed) — never a silent worker
  resurrection and never "worker dead but submissions still report success".
- **Publish outcome failure:** a `FAILED_TRANSPORT`/etc. *outcome* (the publisher's isolated,
  non-raising contract) is counted, not fatal; sustained failures fill the bounded queue and
  surface as explicit overflow.
- **Unknown Redis outcome / retry:** unchanged from D1/ADR-021 — a commit whose result is lost
  may, on retry, append another stream record with the same identity. That is **at-least-once**;
  cross-process dedup is **C1**, not M2.

## Explicit non-guarantees

- **No exactly-once** delivery or processing.
- **Cross-process dedup = NOT_GUARANTEED** (C1).
- M2 does **not** enable IPC in production (nothing composes the boundary; config defaults keep
  `enabled = False`) and does **not** start Phase H.

## Consequences

- **C1** — unblocked (durable/idempotent consumer dedup); still NOT_GUARANTEED until its phase.
- **L1** — untouched; M2 exposes a status object a future L1 may consume.
- **Phase H** — still not started; no container split, dual-run, or cutover.
- **Threading:** the boundary uses `asyncio.Queue` and assumes the ingestion callback and worker
  share one event loop (the production ingestion model). A cross-thread bridge is out of scope.
