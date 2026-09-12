# ADR-023 — Durable Cross-Process Consumer Idempotency for Market IPC

| Field | Value |
|-------|-------|
| **Status** | Proposed |
| **Date** | 2026-09-12 |
| **Deciders** | Platform / Market-Ingestion Decoupling Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | Phase C (shadow consumer), ADR-020 (M1 durable epoch), ADR-021 (D1 atomic publication), ADR-022 (M2 async boundary); DECOUPLING-C1 |

---

## Context

Transport is **at-least-once**: Redis Stream redelivery (`XAUTOCLAIM` of stale pending), an ACK
whose result is lost, and the M2/D1 retry that may append a second stream record with the same
canonical identity all deliver one canonical event more than once. Phase C deduplicated by the
canonical identity `(producer_id, producer_epoch, producer_sequence)` but only in a **bounded
in-memory window**. That window is per-process: a consumer/process restart (or eviction under a
long backlog) loses it, so an already-applied event is re-applied. Carried-forward finding M1
flagged this as the residual exactly-once gap.

## Problem (C1)

Make a **completed application** survive process restart, consumer restart, Redis Stream
redelivery, ACK failure, and the same canonical identity arriving under a **different Redis
Stream (transport) id** — **without** claiming end-to-end exactly-once delivery.

## Decision

Add a durable dedup authority (`app/market_ipc/durable_dedup.py`) behind an async
`Deduplicator` protocol (`contains`/`record` over `ProducerEventIdentity`):

- **`DurableDeduplicator`** — one TTL-bounded Redis key per canonical identity.
  `contains` = `EXISTS` (survives restart); `record` = `SET key 1 EX dedup_ttl_seconds`.
- **`CompositeDeduplicator`** — the durable authority fronted by the bounded in-memory window as
  a hot cache. Correctness comes **solely** from the durable store: a fresh process (empty cache)
  still sees a previously-recorded identity via the durable `contains`. `record` writes the
  durable store **first**, then the cache — a durable failure propagates before the cache is
  warmed.
- **`InMemoryDeduplicator`** — async adapter over the Phase-C window; the default when no durable
  store is wired (non-durable, single-process; tests / not-yet-activated composition).
- **Dedup key** is `f"{prefix}:{len(producer_id)}:{producer_id}:{epoch}:{sequence}"` — the
  producer_id length is prefixed so no two distinct triples can collide regardless of the
  producer_id's contents. The **transport (Redis Stream) id is deliberately excluded**: the
  canonical identity is the dedup identity, so the same event under a different stream id
  deduplicates.

The consumer's dedup is now the async `Deduplicator`; `contains` is checked before apply and
`record` is committed **only after** a successful shadow apply (unchanged apply-then-mark order).

### Rejected alternatives
- **In-memory-only dedup (Phase C)** — the problem itself; lost on restart.
- **Coupling the durable mark atomically with the sink's own state transition** — the current
  `ShadowMarketEventSink` is non-authoritative/in-memory with no durable side effect, so there is
  nothing to make atomic yet. Deferred to when an authoritative sink is connected (Phase H).
- **`SETNX`-as-lock to fake exactly-once** — explicitly forbidden by the C1 spec; it would claim
  a guarantee the transport cannot provide and hide the residual crash window rather than
  document it.
- **Unbounded dedup keys (no TTL)** — storage grows without limit.

## Failure semantics

- **Dedup store failure is fail-closed.** A `RedisError` from `contains` or `record` propagates;
  the consumer counts `dedup_store_failures`, returns `DEDUP_UNAVAILABLE` (a **non-terminal**
  outcome — the entry is **not** ACKed), and **never applies an event without idempotency
  protection**. The entry redelivers and is retried when the store recovers.
- **Residual apply-then-mark crash window.** If the process dies after the shadow apply but
  before the durable `record` commits, redelivery re-applies the event. This is **harmless for
  the current non-authoritative shadow sink** (record/compare only, no durable/business effect).
  It is documented, not hidden.
- **Total Redis data/volume loss** discards the stream **and** this dedup state together —
  durability is exactly the configured Redis persistence, no stronger.

## Retention

`dedup_ttl_seconds` (default 86 400 s = 1 day; bounded 1 h..30 d) caps per-key lifetime so the
dedup keyspace cannot grow without limit. The TTL must exceed the maximum realistic redelivery
horizon (pending-entry idle + restart windows) so a key never expires while its event is still
redeliverable; one trading day comfortably covers the intraday session plus restart slack.

## Explicit non-guarantees

- **No exactly-once** delivery or processing — transport stays at-least-once; C1 is a durable
  **idempotent-consumer** mechanism.
- **No atomic sink+mark** transition — deferred to Phase H's authoritative sink.
- C1 does **not** enable IPC in production (nothing composes a durable deduplicator; config
  defaults keep `enabled = False`), does not change M1/D1/M2 semantics, and does not start
  Phase H, L1, or FIX-2.

## Consequences

- **Carried-forward M1 finding** (restart re-application) — resolved for the durable-wiring path;
  the in-memory default retains the documented single-process limitation until composition wires
  the `CompositeDeduplicator`.
- **Phase H** — unblocked to connect an authoritative comparator sink; that phase owns the
  atomic sink+mark transition this ADR defers.
- **Config** adds `dedup_key_prefix` and `dedup_ttl_seconds` (bounded, validated, no-whitespace),
  inert until the durable path is composed.
