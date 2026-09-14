# Phase-H6 — Backend Restart Independence (Offline / Test-Redis Only)

Governed by **ADR-025** (activation-flag matrix), **ADR-026** (H3 publication order), and
**ADR-023** (durable consumer idempotency). H6 adds evidence only: it keeps one **continuously
alive** market-ingestion incarnation running while the backend consumer is **destroyed and
recreated**, and proves the decoupled path lets a fresh backend resume from Redis without
reconnecting the provider, losing durably-published events, or reapplying durably-completed events.
No new architecture, no new config, no ADR, **no production-code change**.

> **H6 DOES NOT USE REAL DHAN. H6 DOES NOT CONTACT PRODUCTION. H6 DOES NOT ACTIVATE THE CONSUMER
> OR PUBLISHER IN PRODUCTION. H6 DOES NOT MAKE IPC AUTHORITATIVE. H6 DOES NOT DRIVE THE TICKENGINE
> OR MARKETCONTEXT. H6 DOES NOT IMPLEMENT/SOLVE B2 / B4 / B11. H6 DOES NOT IMPLEMENT FIX-2 OR TOUCH
> FIX-2A PR #63. H6 DOES NOT PERFORM CUTOVER. H6 DOES NOT RESTART INGESTION (a new producer epoch
> belongs to H7). H6 DOES NOT START H7.**

## Topology

```
deterministic gated provider (BrokerAdapter + LiveMarketDataAdapter; no Dhan/tokens/sockets/internet)
        │  MarketIngestionService + ProviderSupervisor  (ONE incarnation, stays up continuously)
        ▼
PublishingEventSink.handle → M2 AsyncPublicationBoundary.submit → ordered worker
        │
        ▼
MarketEventPublisher.transmit → D1 RedisAtomicPublisher   (M1 identity fixed at submit)
        │        └─ L1 FeedContinuityTracker observes accepted/published/breaks
        ▼
Redis md:events  (+ md:reference:<date>)   [disposable redislite]   ← the DECOUPLING BUFFER
        ▼
backend consumer A ──destroyed──▶ backend consumer B ──destroyed──▶ backend consumer C ...
   (own Redis client, EMPTY memory dedup cache, distinct consumer name, own sink per incarnation;
    SHARED durable C1 authority + SHARED Redis consumer group)
        ▼
RecordingShadowSink per incarnation  (NON-AUTHORITATIVE)
        ▼
shadow_compare.compare(expected_views, UNION of all incarnations' applied_views) → ParityReport
```

The producer side is the real composition (`build_publication_stack` + `PublishingEventSink` +
`MarketIngestionService`, publisher mode `INGESTION_SHADOW_PUBLISH`). The only H6-specific test
scaffolding is a **gated provider**: `stream_market_data` is entered exactly once and blocks on an
internal queue between batches, so the ingestion incarnation stays "connected" across every backend
restart while the test controls the feed on demand.

## Ingestion independence (why no production change was needed)

The producer and the consumer share **only** Redis. The backend consumer holds no reference to the
provider, the `MarketIngestionService`, or the M1 `DurableEpochAllocator`; the service holds no
reference to any consumer. Restarting the consumer therefore **cannot**, by construction, reconnect
the provider or allocate a new producer epoch. H6 is the evidence that this architectural property
holds end to end with real components — it is a tests + evidence phase (§33), not an implementation
phase. No genuine lifecycle defect was found.

## Backend restart lifecycle

Each backend incarnation is a genuinely separate runtime object (§10, §30):

- its own `redis.asyncio.Redis` client (separate connection pool),
- a **fresh** in-memory dedup cache (`BoundedDeduplicator`),
- a distinct consumer name (`backend-a`, `backend-b`, …) in the **same** consumer group,
- its own `RecordingShadowSink`.

"Restart" = close the old incarnation's client and construct a new one. Correctness comes **only**
from the durable Redis C1 authority and the shared consumer group, never from process memory —
proven by the empty-cache-no-reapply tests (T09/T10). Process-level (OS subprocess) isolation would
add no new correctness dimension here because every piece of cross-incarnation state lives in Redis,
not in process memory; separate clients + fresh caches already exercise that boundary. Adding a
subprocess harness would require test-only OS orchestration with no new coverage, so it is
deliberately out of scope (§30).

## Redis buffering (the decoupling buffer)

While no backend exists, ingestion keeps publishing and the Redis stream grows (T07: stream length
before downtime < after; the destroyed backend's applied count is unchanged). A fresh backend then
catches up from the stream/consumer group — **no provider replay** is required (`stream_calls == 1`
throughout, T07/T08/T16).

## Consumer group + PEL recovery

- **Group continuity (§22):** a new backend calls `ensure_group` (`XGROUP CREATE … MKSTREAM`,
  `BUSYGROUP` swallowed) — idempotent and non-destructive: it never recreates the stream, resets the
  offset, `XGROUP SETID`s, or deletes the PEL. The shared group offset means backend B reads only the
  un-consumed backlog via `>`, not batch A again.
- **Consumer identity (§23):** the repository's existing model — a per-incarnation consumer name in
  the shared group; an abandoned incarnation's PEL is reclaimed by the next via idle-based
  `XAUTOCLAIM` (`claim_idle_ms`). No new distributed identity scheme is introduced.
- **Abandoned PEL recovery (T11):** backend A reads entries into its PEL then disappears without
  acking; backend B reclaims them via `XAUTOCLAIM` after the idle threshold and the PEL drains to 0.

## Durable C1 restart semantics

The durable dedup key is `md:dedup:<len>:<producer_id>:<epoch>:<sequence>` — keyed on the canonical
identity, **never** on the consumer name or the Redis Stream id. A fresh backend with an empty memory
cache therefore sees a previously-completed identity via the durable `EXISTS` and suppresses it
(T09/T10). An ACK lost before restart is not reapplied because the durable mark already committed
(T12).

## Producer epoch stability

One ingestion incarnation ⇒ one `DurableEpochAllocator` allocation. The producer epoch is fixed at
service start and never changes across any number of backend restarts (T02/T03: epoch unchanged after
each of 3 restarts; T19/T22: unchanged across 30). The producer sequence **continues** across backend
downtime (T04: 1..30 before → 31..55 after; never resets). A stream that already carries multiple
producer epochs (an ingestion restart that happened *before* the H6 window — H6 itself never restarts
ingestion) is consumed correctly, with same-sequence/distinct-epoch events kept distinct (T15).

## Provider lifecycle stability

A backend restart never touches the provider: `connect_calls == 1`, `disconnect_calls == 0`,
`stream_calls == 1` across every restart (T05/T06/T24). The single provider disconnect happens once,
at ingestion shutdown — never because a backend restarted. This models the future property that a
backend deploy/restart must not reconnect Dhan; **it is not a claim against real Dhan.**

## L1 independence

L1 continuity stays `HEALTHY` while the backend is absent and ingestion continues publishing
(T23: `publication_failure_total == 0`, `overflow_total == 0`, service `running`). Backend absence is
not a producer-side break; only a real publication fault (overflow / D1 failure) breaks L1, which is
H5's concern.

## Backlog / catch-up evidence (§25/§36)

`test_h6_t16_t17_large_backlog_catchup_with_concurrent_traffic`:

| stage | events | note |
|-------|--------|------|
| batch A (consumed normally) | 2,000 | backend A applies, then stops |
| batch B (published while backend down) | 5,000 | accumulates in Redis (stream length 7,000) |
| batch C (published DURING catch-up) | 2,000 | concurrent new traffic (§26) |
| **total logical events** | **9,000** | |
| parity `matched_total` | 9,000 | clean; missing/unexpected/mismatch = 0 |
| `provider.stream_calls` | 1 | no provider replay during catch-up |

Backend B drains the 7,000-event backlog while ingestion publishes batch C concurrently; the run
converges with neither the backlog nor the new traffic starved. `read_count = 1,000`,
`publish_queue_capacity = 20,000` (headroom over peak in-flight). This is correctness evidence, **not
a production performance benchmark** — wall-clock and cycle counts are test-environment artefacts.

## Restart stress + resource bounds (§15/§31)

`test_h6_t19_t20_t21_t22_many_restart_cycles_no_leak` runs **30** backend restart cycles (satisfies
T19 ≥5 and T20 25–50) against one live ingestion incarnation. After a warm-up, across the remaining
cycles it asserts (steady-state ceilings captured at cycle 2):

- `len(asyncio.all_tasks())` does not grow (no consumer/task accumulation — T21),
- server-side `connected_clients` (`INFO clients`) does not grow (no Redis client accumulation — T22),
- `provider.connect_calls / disconnect_calls / stream_calls` unchanged, producer epoch unchanged,
- final parity over all 30 cycles is clean with `known_b2_duplicate_total == 0`.

## Failure scenarios (all fail-closed / surfaced, never a false clean parity)

| scenario | test | evidence |
|----------|------|----------|
| clean restart, ingestion alive | T01/T18 | parity A+B+C clean after restart + concurrent batch C |
| events accumulate while backend down | T07 | stream grew; destroyed backend's applied count unchanged |
| fresh backend catches up backlog | T08 | 150-event backlog drained to clean parity, no provider replay |
| durable survives recreation / empty cache no reapply | T09/T10 | redelivered identities all suppressed, `applied == 0` |
| abandoned PEL reclaimed | T11 | `XAUTOCLAIM` recovers 40 stranded entries, PEL drains to 0 |
| ACK lost before restart | T12 | durable mark recognised → `known_b2_duplicate == 0`, no reapply |
| B2 apply→mark crash | T13 | mark crashes → reclaim reapplies → `known_b2_duplicate == 1`, not clean |
| legal producer-sequence gap | T14 | seq-2 dropped at D1; fresh backend consumes, no `MISSING` |
| multi-epoch stream | T15 | epoch-1 and epoch-2 events both consumed, distinct, clean |

## B2 / B4 / B11 limitations (unchanged; surfaced, not solved)

- **B2** (apply→mark atomicity) = `DESIGN_RESOLVED_IMPLEMENTATION_PENDING` — H8A. Demonstrated and
  counted across restart here (T13); **not** solved in H6.
- **B4** (stream retention vs dedup/redelivery horizon) = `DESIGN_RESOLVED_IMPLEMENTATION_PENDING` —
  H8B. H6 proves restart over **bounded test downtime** only; it proves nothing about Redis stream
  retention or C1 TTL safety for arbitrary production outage durations.
- **B11** (Redis durability / stream-loss detection) = `DESIGN_RESOLVED_IMPLEMENTATION_PENDING` —
  H8C. H6 assumes the test Redis survives; it does not prove Redis-loss detection or durable
  infrastructure guarantees.

## Reference-bootstrap status (§21)

`BACKEND_REFERENCE_BOOTSTRAP_PROVEN = NO` (unchanged from H5). H6 backend restart consumes only
**stream** events; it does not load the compacted `md:reference:<date>` hash into a starting
consumer, and it makes no new reference-bootstrap architecture decision. Left for the appropriate
later phase.

## Timestamp / FIX-2 separation (§29)

Fixtures use canonical tz-aware **UTC** timestamps; **no** `+5:30`/`-5:30` workaround (T25/T29).
Dhan LTT parsing is neither tested nor changed. `LIVE_DHAN_TIMESTAMP_PARITY = NOT_PROVEN`; FIX-2A
remains `RC3_CONFIRMED = INCONCLUSIVE` and untouched.

## Required test matrix (§34)

| id | test | covered by |
|----|------|-----------|
| T01 / T18 | clean backend restart; final clean parity | `t01_t18_clean_backend_restart_while_ingestion_runs` |
| T02 / T03 / T24 | producer id/epoch + incarnation stable | `t02_t03_t24_producer_identity_stable_across_backend_restart` |
| T04 | sequence continues across downtime | `t04_sequence_continues_across_backend_downtime` |
| T05 / T06 | provider connect/disconnect unchanged | `t05_t06_provider_lifecycle_independent_of_backend_restart` |
| T07 | events accumulate in Redis while absent | `t07_events_accumulate_in_redis_while_backend_absent` |
| T08 | fresh backend catches up backlog | `t08_fresh_backend_catches_up_backlog` |
| T09 / T10 | durable survives; empty cache no reapply | `t09_t10_durable_survives_recreation_empty_cache_no_reapply` |
| T11 | dead-consumer PEL reclaimed | `t11_dead_consumer_pel_reclaimed_by_new_backend` |
| T12 | ACK-lost not reapplied | `t12_ack_lost_before_restart_not_reapplied` |
| T13 | B2 apply→mark crash unsafe | `t13_b2_apply_mark_crash_remains_unsafe` |
| T14 | legal producer-sequence gap tolerated | `t14_legal_sequence_gap_tolerated` |
| T15 | multi-epoch stream handled | `t15_multiple_producer_epochs_in_stream_handled` |
| T16 / T17 | ≥5,000 backlog + concurrent catch-up traffic | `t16_t17_large_backlog_catchup_with_concurrent_traffic` |
| T19 / T20 / T21 / T22 | 30 restart cycles, no task/client leak | `t19_t20_t21_t22_many_restart_cycles_no_leak` |
| T23 | L1 unaffected by backend-only restart | `t23_l1_healthy_during_backend_downtime` |
| T25 / T28 / T29 | no broker construction; shadow sink only; canonical UTC | `t25_t29_no_broker_construction_and_canonical_utc` |
| T26 / T27 | no TickEngine/MarketContext mutation | arch import test (structural) |
| T30 | H5 focused topology remains green | full-suite regression gate |

T25–T29 are additionally proven structurally by
`tests/architecture/test_market_ipc_import_boundary.py::test_h6_backend_restart_topology_touches_no_authority_broker_or_fix_surface`
(the H6 topology imports no authority/broker/FIX surface). T30 is a regression gate satisfied by the
H5 suite and the full suite.

## H7 boundary (§28)

H6 proves **backend** restart only. **Ingestion** restart / a new producer epoch under a cutover
topology belongs to H7 and is explicitly out of H6 scope. H6 PASS means `READY_FOR_H7 = YES` only;
`READY_FOR_LIVE_CUTOVER = NO` and `READY_FOR_IPC_AUTHORITY = NO`: ingestion-restart cutover (H7),
B2/B4/B11 (H8), single-owner interlock/transfer/authority verification (H9), and live Dhan timestamp
parity (FIX-2) all remain open. `SINGLE_DHAN_OWNER = TRUE` is trivially preserved (no real Dhan; no
second account introduced).
