# Phase-H4A — IPC Consumer Composition + Durable C1 Wiring (Offline / Test-Redis Only)

Governed by **ADR-023** (durable consumer idempotency) and **ADR-025** (activation flag matrix).
H4A composes the backend-side Redis Streams consumer into a long-lived runtime and wires the
durable C1 dedup authority into it — proven only against isolated `redislite` and canonical
fixtures.

> **H4A DOES NOT USE REAL DHAN. H4A DOES NOT CONTACT PRODUCTION. H4A DOES NOT ACTIVATE THE
> CONSUMER IN PRODUCTION. H4A DOES NOT MAKE IPC AUTHORITATIVE. H4A DOES NOT TOUCH TICKENGINE
> AUTHORITY. H4A DOES NOT IMPLEMENT FIX-2 OR TOUCH THE FIX-2A BRANCH/PR.**

## Topology

```
md:events (Redis Stream)
      │  XREADGROUP (>) + bounded XAUTOCLAIM reclaim per cycle
      ▼
MarketEventConsumer  ── decode envelope → gate (trading-date, universe) → durable C1 dedup
      │                   → decode payload → apply → durable mark → XACK
      ▼
ShadowMarketEventSink (RecordingShadowSink) — NON-AUTHORITATIVE (record/compare only)
```

The composition boundary is **`app/market_ipc/consumer_runtime.py`**:

- **`MarketEventConsumerRuntime`** — owns one Redis client + the `MarketEventConsumer` + a single
  cancellable poll-loop task. It never places orchestration in route handlers, the TickEngine, or
  the strategy layer.
- **`compose_consumer_runtime(settings, …)`** — the only builder. It derives the ADR-025 mode from
  the flags; `SHADOW_CONSUME_COMPARE` composes a live runtime over `Redis.from_url(redis_url)` with
  a durable `CompositeDeduplicator`, and every other legal flag shape returns an inert (disabled)
  runtime that owns no Redis client. Illegal flag shapes are rejected fail-closed by the frozen
  `validate_phase_h_flags` matrix.

The consumer itself (`MarketEventConsumer`, `CompositeDeduplicator`) already existed (Phase C/C1);
H4A adds only the process lifecycle around it.

## Configuration (ADR-025)

The one legal H4A composition is `MarketPathMode.SHADOW_CONSUME_COMPARE`:

| flag | value |
|------|-------|
| `market_ingestion_service_enabled` | false |
| `ipc_publisher_enabled` | false |
| `ipc_consumer_enabled` | true |
| `ipc_shadow_compare_enabled` | true |
| `ipc_authoritative_enabled` | false |
| `legacy_market_path_enabled` | true |

A consumer with neither a shadow-compare nor an authority role is **illegal** (it would drain the
stream and record dedup keys, poisoning dedup for a later authoritative switch); the ADR-025 matrix
already rejects it, so no new flag or ADR is introduced. Defaults derive `LEGACY_ONLY`, so nothing
in default configuration or default Compose composes a live consumer.

## Lifecycle

```
compose (create Redis client, build durable C1 + shadow sink + consumer)
   → start(): ensure consumer group (idempotent XGROUP CREATE, mkstream) → launch poll loop → RUNNING
   → poll loop: bounded XAUTOCLAIM reclaim + XREADGROUP, one entry to a terminal outcome at a time
   → stop(): STOPPING → cancel loop (wakes a blocked read) → close Redis client once → STOPPED
```

- **Readiness** is the running poll loop (`is_ready == state is RUNNING`), never merely that the
  process exists.
- **Startup failure** (e.g. Redis unreachable so the group cannot be ensured): the state is
  `FAILED`, the owned Redis client is closed, no background task is left, and the error propagates.
  The runtime never claims READY on a partial start.
- **Shutdown**: a blocked `XREADGROUP` is cancelled rather than waited out; the Redis client is
  closed exactly once (idempotent `stop()`); no task leaks.

## C1 ordering and the ACK contract

For a **new** event: durable duplicate check → shadow apply → durable dedup mark → XACK. The
durable mark is written **only after** a successful apply, so the dedup key can never permanently
suppress an event that was not applied.

For a **duplicate** (durable `contains` is true): do not reapply → XACK.

XACK happens only when a terminal outcome is reached — either a durably-known duplicate, or a new
event whose apply **and** durable mark both succeeded, or a permanently-invalid entry (malformed /
unsupported schema / wrong kind / stale-or-future trading date / non-matching universe) that is
terminally ACKed so a poison message cannot jam the group. Transient failures (sink failure, dedup
store unavailable, read/ACK failure) are **never** ACKed — the entry stays pending and redelivers.

## Durable-vs-memory authority

Correctness comes solely from the durable Redis store (`DurableDeduplicator`: one TTL-bounded key
per canonical identity). The in-memory `BoundedDeduplicator` inside `CompositeDeduplicator` is only
a hot cache: a fresh runtime/process with an empty cache still recognises a previously-applied
identity via the durable `EXISTS`. `record` writes durable first; if that raises, the cache is not
polluted and the caller fails closed. Dedup identity is `(producer_id, producer_epoch,
producer_sequence)` — a new epoch is a new producer incarnation, so the same sequence under a new
epoch is not a duplicate.

## Failure behavior (fail-closed)

- **Durable dedup store unavailable** — do not ACK, do not apply blind; the entry is left pending
  and the failure is counted (`dedup_store_failures`).
- **Sink apply fails** — not ACKed; redelivered and retried (`sink_failures`).
- **Redis read/claim fails mid-run** — counted (`read_failures`); the cycle ends and the loop backs
  off. `poll_once` never raises, so a Redis fault cannot kill the loop.
- **XACK fails** — counted (`ack_failures`); the entry redelivers.

Observability is bounded (fixed-field counters, no per-instrument/per-event cardinality) via
`ConsumerDiagnostics`.

## B2 residual window (NOT resolved here)

Delivery is **at-least-once with durable idempotency**, not exactly-once. The residual window is:
sink apply succeeds → process crashes → the durable mark has not committed → redelivery re-applies.
For the non-authoritative shadow sink this is harmless (no durable/business side effect). H4A tests
and this document expose the window; it is **not** solved here. B2 remains
`DESIGN_RESOLVED_IMPLEMENTATION_PENDING`. Atomically coupling the durable mark with a future
*authoritative* sink's own state transition is deferred to when that sink is connected (ADR-023).

## H4B boundary

H4B owns pending-entry recovery hardening, deeper XAUTOCLAIM/abandoned-consumer recovery, and
poison/pending management. The runtime already performs a bounded per-cycle reclaim (inherited from
the Phase-C consumer), but H4A does **not** claim that hardening complete and adds no new recovery
mechanism. B4 (stream retention vs dedup horizon) and B11 (Redis durability / loss detector) remain
later work; H4A uses short local test settings and invents no sequence-arithmetic gap detection —
legal producer sequence gaps (a sequence allocated before enqueue, then dropped on bounded-queue
overflow) are tolerated, never treated as loss.

## FIX-2 separation

H4A uses deterministic fixture timestamps and does not depend on live provider timestamp
correctness. FIX-2A is `RC3_CONFIRMED = INCONCLUSIVE`, so H4A claims no live timestamp parity, live
Dhan correctness, or live session-date correctness — only consumer composition/idempotency
correctness for canonical fixture events.

## Authority explicitly OFF

The runtime never drives the TickEngine, MarketContext, sector runtime, or strategies; never
activates a Dhan provider or the IPC publisher; never allocates a producer epoch; and is not wired
into backend startup. Architecture tests enforce this: no module outside `app/market_ipc`
constructs the consumer or composes the runtime, and `consumer_runtime.py` imports no Dhan /
publisher / producer-epoch surface.
