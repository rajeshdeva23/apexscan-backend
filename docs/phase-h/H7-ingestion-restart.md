# H7 — Market-ingestion restart / new producer-epoch validation

**Phase:** DECOUPLING H7 (offline test topology only)
**Base:** `feature/decoupling-hardening` @ `0d95121` (H6 PASS)
**Scope:** prove the market-ingestion service can terminate and be recreated as a genuinely new
producer incarnation while the backend consumer survives/restarts independently and Redis preserves
the event boundary. **No production change.** OFFLINE ONLY — no real Dhan, no production, no
authority.

H7 is the complement of H6:

| | who restarts | producer epoch | provider lifecycle |
|---|---|---|---|
| **H6** | the **backend** consumer | unchanged (one incarnation) | never reconnected |
| **H7** | the **ingestion** service | **strictly increases** per incarnation | recreated per incarnation |

## Why no production code changed

The durable producer epoch (M1, `app/market_ipc/epoch.py`) is already the load-bearing mechanism.
`DurableEpochAllocator` persists a monotonic counter to a producer-local, crash-safe file
(`<state_dir>/producer-epoch-<producer_id>.json`) and advances+fsyncs it **before** returning an
epoch. The epoch is allocated exactly once per incarnation, by `publisher.start()` (invoked from
`boundary.start()` inside `MarketIngestionService._start_publication`), **before** the provider
connects. A new incarnation on the same durable state directory therefore reads the prior epoch and
returns a strictly higher one — by construction, with no new code. H7 is tests + evidence + one
arch-purity assertion.

## Topology

```
deterministic gated provider (no Dhan / tokens / sockets / internet)
    -> MarketIngestionService A         producer_id P, producer_epoch E, sequence 1..N
    -> M1 DurableEpochAllocator -> M2 AsyncPublicationBoundary -> D1 RedisAtomicPublisher -> L1
    -> Redis md:events (+ md:reference:<trading_date>)
    ... service A stops / faults / is abruptly lost ...
    -> MarketIngestionService B         producer_id P, producer_epoch > E, sequence RESET 1..M
    -> backend MarketEventConsumer + durable C1 -> RecordingShadowSink -> H4C comparator
```

## Producer incarnation

A *producer incarnation* is one lifetime of `publisher.start()` → publishing → shutdown. Each
incarnation:

- keeps a **fixed** `producer_id` (`market-ingestion`);
- allocates **one** `producer_epoch` at start, higher than any previous incarnation on the same
  durable volume;
- numbers its events `producer_sequence = 1, 2, 3, …`, **reset to 1** at the start of the epoch.

The dedup identity is `(producer_id, producer_epoch, producer_sequence)`. Because the epoch is part
of the identity, a post-restart `seq=1` under a new epoch is a *different* identity from the prior
incarnation's `seq=1` — they never collide in C1.

## Epoch allocation and sequence reset

- **Allocation order (frozen H3):** config → Redis → D1 → M2 → L1 → provider **last**. The epoch is
  allocated (and the stream group ensured) inside `publisher.start()`, before the provider connects.
- **Sequence reset:** `producer_sequence` is incarnation-local and restarts at 1 per epoch. Epoch
  20 / seq 1 and epoch 21 / seq 1 are distinct identities; C1 applies both.
- **Monotonicity:** a crash between allocation and use can only *skip* an epoch (a harmless gap),
  never reuse one. Concurrent starts sharing a `producer_id` are serialised by an exclusive file
  lock and each receive a distinct epoch.

## Provider reconnect vs. service restart

These are deliberately different and both proven (T14/T15/T30):

- **Provider transport reconnect** (the `ProviderSupervisor` re-iterating `stream_market_data`
  after a recoverable drop) happens **within one incarnation**: it re-enters the stream
  (`stream_calls` grows) but never re-runs `publisher.start()`, so the **epoch is unchanged** and
  `connect_calls` stays 1 (the socket is not reconnected through the coordinator). L1 goes
  `PROVIDER_DEGRADED` and heals to `HEALTHY` only on fresh publication evidence — a reconnect alone
  is not recovery.
- **Genuine service restart** (a new `MarketIngestionService` on the same durable state) allocates a
  **strictly higher epoch**.

## Clean vs. abrupt restart

- **Clean shutdown** (`service.stop()`): provider intake stops, M2 drains under a bounded timeout,
  L1 is finalised (`CLEAN_SHUTDOWN`), and the owned Redis client is closed. Accepted events are not
  abandoned (T01 consumes every event across a clean restart).
- **Abrupt loss:** an abrupt process loss can leave a sequence *allocated but never published* (the
  D1 transmit never reached Redis). This is a **legal sequence gap** — modelled in T19 by a D1
  transmit failure at seq 2 — and it is preserved, never reconstructed or fabricated. The next
  incarnation still allocates a higher epoch because the epoch was persisted at allocation time,
  before any event. Total durable-volume loss is explicitly **outside** the M1 supported failure
  model (documented in `epoch.py`, ADR-020); H7 does not pretend otherwise.

## Redis / consumer group / reference continuity

Redis stays alive across an ingestion restart. `md:events`, its consumer group, and the compacted
`md:reference:<trading_date>` hash are **never** deleted or recreated (`ensure_group` swallows
`BUSYGROUP` idempotently). Epoch-A events remain consumable while epoch B begins publishing; D1
reference semantics stay intact across epochs (T08/T09/T13).

## C1 behaviour across epochs

- Same seq under different epochs both apply; replaying an epoch's entries is suppressed durably
  (T05/T06).
- An old-epoch entry stranded in a dead backend's PEL is reclaimed via `XAUTOCLAIM` and coexists
  with new-epoch traffic — no identity collision (T10).
- An ACK lost for an old-epoch event is suppressed by durable C1 on reclaim; a same-seq new-epoch
  event still applies (T11).
- Correctness comes solely from the durable Redis authority; a fresh backend's empty in-memory
  cache never causes a reapply.

## L1 behaviour

Each incarnation gets its own L1 lifecycle. `producer_started` resets a prior terminal break **only
on an epoch change** (a new incarnation), never on a provider reconnect (same epoch is idempotent).
A terminal publication break (overflow / worker fault / publish failure) is sticky for the
incarnation: a clean drain never hides it, and it is cleared only by a new epoch (T16). A restart
after a terminal failure is a new incarnation with a higher epoch; the old incarnation's terminal
state is not cleared in place.

## Failure before the provider

Because the epoch and the stream group are established before the provider connects, any startup
failure in that window fails closed and the **provider never starts** (`connect_calls == 0`):

- epoch allocation failure (T17a);
- Redis unavailable at `ensure_group` (T17b);
- corrupt durable epoch state → `EpochStateError`, never a silent low-epoch reset (T18).

## Test matrix

| test | proves |
|---|---|
| T01/T02/T03/T04/T07/T08/T09 | clean restart: id stable, epoch increases, seq resets, backend alive, stream+group survive |
| T05/T06 | same seq / new epoch both apply; replaying an epoch is suppressed |
| T10 | old-epoch PEL entry reclaimed + new-epoch traffic coexist |
| T11 | ACK-lost old-epoch suppressed; same-seq new-epoch applies |
| T12 | B2 apply→mark crash surfaces a duplicate across the restart (not solved) |
| T13 | D1 reference semantics intact across epochs |
| T14/T15/T30 | provider reconnect keeps the epoch; genuine restart bumps it |
| T16 | terminal publication failure → new incarnation gets a higher epoch |
| T17 | epoch-allocation / Redis failure before provider → provider never starts |
| T18 | corrupt epoch state fails closed (no silent reset) |
| T19 | legal sequence gap from an abrupt in-flight loss is preserved across a restart |
| T20 | ≥25 incarnations → strictly monotonic, never-reused epochs |
| T21 | concurrent allocation on one durable dir → unique, monotonic epochs (M1 file lock) |
| T22 | ≥10,000-event two-incarnation replay clean (seq resets under the new epoch) |
| T23 | ≥10-incarnation multi-restart replay clean; all identities unique |
| T24/T25/T26/T27 | repeated restarts leak no tasks, Redis clients, provider tasks, or M2 workers |
| T28 | H4/H5/H6 regression green (full suite) |
| T29/T30/T31/T32 | no Dhan construction, no authority, canonical tz-aware UTC (no −5:30) |

Real Redis via `redislite` exercises `XADD`, `XGROUP`, `XREADGROUP`, `XPENDING`, `XAUTOCLAIM`,
`XACK`, durable C1, and the reference hash. The arch test
`test_h7_ingestion_restart_topology_touches_no_authority_broker_or_fix_surface` proves import
purity (no Dhan / market_engine / market_intelligence / strategies / pyotp / websockets).

## B2 / B4 / B11 status

- **B2** (apply→mark window): `DESIGN_RESOLVED / IMPLEMENTATION_PENDING`. Demonstrated across a
  restart (T12); **not** solved here (H8A).
- **B4** (retention / dedup horizon): `DESIGN_RESOLVED / IMPLEMENTATION_PENDING`. Test retention is
  bounded/test-only; no production retention claim (H8B).
- **B11** (durable Redis loss detection): `DESIGN_RESOLVED / IMPLEMENTATION_PENDING`. Redis is
  assumed available/persistent for healthy scenarios; H7 establishes no Redis-loss detection (H8C).

## FIX-2 limitation

Fixture timestamps are canonical tz-aware UTC with **no −5:30 workaround**. H7 does **not** prove
live Dhan timestamp parity (`LIVE_DHAN_TIMESTAMP_PARITY = NOT_PROVEN`; FIX-2A remains INCONCLUSIVE
on PR #63).

## H8 boundary

A successful H7 allows H8 to begin, but H8 is not one implementation: H8A (B2 closure), H8B (B4
closure), H8C (B11 closure), H8D (authority-readiness review). None starts automatically. H7 does
not touch production, real Dhan, IPC authority, or the cutover.
