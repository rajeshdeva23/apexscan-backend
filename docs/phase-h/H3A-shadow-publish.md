# Phase-H3A — Shadow-Publish Implementation (Fake/Replay Provider + Test Redis)

Governed by **ADR-026**. H3A implements the decoupled shadow-publish pipeline and proves it against
a fake/replay provider and a real **test** Redis — with **no real Dhan, no consumer, no C1, no
TickEngine authority, and no production contact**.

> **H3A DOES NOT CONNECT TO REAL DHAN. H3A DOES NOT ACTIVATE PRODUCTION SHADOW PUBLISH. H3A DOES
> NOT ENABLE THE IPC CONSUMER. H3A DOES NOT CHANGE BACKEND AUTHORITY.**

## Pipeline

```
fake/replay provider → PublishingEventSink → M2 boundary → D1 publisher → test Redis
                                              ↑ L1 continuity observes accepted/published/terminal
```

## What H3A adds

- **`app/market_ingestion/publication.py`** — `build_publication_stack()` wires M1
  (`DurableEpochAllocator`) + D1 (`RedisAtomicPublisher` + `MarketEventPublisher`) + M2
  (`AsyncPublicationBoundary`) + L1 (`FeedContinuityTracker`) + `PublishingEventSink`. Building the
  stack performs no Redis I/O and no epoch allocation.
- **`PublishingEventSink`** — `handle(datum)` calls `M2.submit` (O(1), non-blocking, no Redis/D1/
  TickEngine call); on `ENQUEUED` it records the just-allocated producer_sequence as the L1 accepted
  position; on `REJECTED_OVERFLOW`/`REJECTED_NOT_RUNNING` it raises `PublicationTerminalError`
  (fail closed).
- **`app/market_ingestion/errors.py`** — `PublicationTerminalError` (import-pure).
- **`service.py` publisher mode** — the frozen startup order: `boundary.start()` (allocates the M1
  epoch via `publisher.start()`) → L1 `producer_started` → bounded L1 observer → provider connect →
  supervisor. Fail-closed actuation: a terminal break (overflow via the sink, or a worker fault
  seen by the observer) trips a single terminal watcher that stops intake, disconnects the provider,
  and marks the service `FAILED`. The supervisor re-raises `PublicationTerminalError` ahead of its
  generic reconnect catch-all, so a terminal break never self-heals into a reconnect loop.
- **`composition.py`** — publisher-mode branch builds the stack from settings (lazy; live-gated).
- **`Settings`** — the ADR-026 live interlock: `IPC_PUBLISHER_ENABLED=true` requires
  `LIVE_H3_PUBLISH_APPROVED=true` (default `false`), so a deployed publisher-on process cannot start
  accidentally before the B6 live sub-decision. Tests inject the service directly (structural
  fake-vs-real distinction), never disabling validation.
- **`market_ipc/publisher.py`** — a read-only `current_sequence` property (pure observability, no D1
  semantic change) so the sink can record the accepted sequence without building `diagnostics()`.

## Startup / shutdown order

- **Startup**: config → `boundary.start()` (M1 epoch) → L1 `producer_started` → observer → provider
  connect → subscribe → RUNNING. The provider never starts if the publication infrastructure fails
  (e.g. Redis unavailable → `boundary.start()` raises → `FAILED`, provider never connected).
- **Shutdown**: stop provider intake → disconnect provider → M2 bounded drain → L1 records
  clean/incomplete drain → stop observer/watcher → STOPPED.

## Failure policy (fail closed)

Queue overflow, M2 worker fault, and terminal D1/Redis publication failure are terminal for the
producer incarnation: provider intake stops, the provider disconnects, and the service reports
`FAILED`. No silent drop/coalesce; overflow is always explicit.

## Positions

The L1 **accepted** position is tracked at sequence granularity (from `publisher.current_sequence`
on `ENQUEUED`). The **published** position is tracked at count granularity (`published_total` from
M2 diagnostics) in H3A; a sequence-level published position would require a new publisher diagnostic
that H3A deliberately does not add (it would touch the D1 transmit path). Under backlog,
`last_accepted_sequence` exceeds the confirmed `published_total`, demonstrating that queue
acceptance is not Redis durability.

## Configuration

H3A test shape: `market_ingestion_service_enabled=true`, `ipc_publisher_enabled=true`,
`ipc_consumer_enabled=false`, `ipc_shadow_compare_enabled=false`, `ipc_authoritative_enabled=false`,
`legacy_market_path_enabled=true` (+ `market_provider_enabled=false` in the ingestion process for
the single-owner guard). Derives `INGESTION_SHADOW_PUBLISH`. `live_h3_publish_approved` stays
`false`, so only dependency-injected fake providers run — a real Dhan provider + publisher cannot
start.

## Verification

Integration (redislite): canonical Tick/Quote/MarketReference land in the stream in FIFO order with
monotonic identity; MarketReference commits the compacted reference hash atomically (D1); the M1
epoch is allocated once and a new incarnation allocates a new epoch; clean drain publishes
everything. Unit: publishing-sink outcome mapping; provider-never-starts-if-infra-fails;
startup order; overflow fail-closed + provider disconnect; observer trips on worker fault; clean
drain finalizes L1; the live interlock. `docker compose config` NOT executed locally (no Docker);
compose remains profile-gated with no public port.

## Scope

No real Dhan, no IPC consumer, no C1, no L1 authority over the backend, no shadow-consume, no
cutover, no H3B/H4, no FIX-2, no production contact. Backend default stays LEGACY_ONLY.
M1/D1/M2/C1/L1 failure models unchanged. `B6_DECISION = NO_LIVE_H3_YET` remains — H3A is not live.
