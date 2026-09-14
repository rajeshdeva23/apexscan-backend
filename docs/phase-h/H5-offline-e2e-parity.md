# Phase-H5 — Offline End-to-End IPC Parity + Cutover-Evidence (Offline / Test-Redis Only)

Governed by **ADR-025** (activation-flag matrix), **ADR-026** (H3 publication order), and
**ADR-023** (durable consumer idempotency). H5 adds evidence only: it wires the already-built
producer (M1/M2/D1/L1) and consumer (H4A/H4B/C1) subsystems into one **offline** pipeline, drives
canonical fixtures through a deterministic fake provider, and proves the decoupled path preserves
canonical semantics on replay. No new architecture, no new config, no ADR.

> **H5 DOES NOT USE REAL DHAN. H5 DOES NOT CONTACT PRODUCTION. H5 DOES NOT ACTIVATE THE CONSUMER
> OR PUBLISHER IN PRODUCTION. H5 DOES NOT MAKE IPC AUTHORITATIVE. H5 DOES NOT DRIVE THE TICKENGINE
> OR MARKETCONTEXT. H5 DOES NOT IMPLEMENT/SOLVE B2 / B4 / B11. H5 DOES NOT IMPLEMENT FIX-2 OR TOUCH
> FIX-2A PR #63. H5 DOES NOT PERFORM CUTOVER. H5 DOES NOT START H6.**

## Topology

```
deterministic fake provider (BrokerAdapter + LiveMarketDataAdapter; no Dhan/tokens/sockets/internet)
        │  MarketIngestionService + ProviderSupervisor  (real composition, publisher mode)
        ▼
PublishingEventSink.handle → M2 AsyncPublicationBoundary.submit   (O(1), no Redis on hot path)
        │  ordered worker
        ▼
MarketEventPublisher.transmit → D1 RedisAtomicPublisher           (M1 identity fixed at submit)
        │        └─ L1 FeedContinuityTracker observes accepted/published/breaks
        ▼
Redis md:events  (+ md:reference:<trading_date> for STREAM_PLUS_REFERENCE)  [disposable redislite]
        ▼
MarketEventConsumer (H4A poll_once: XAUTOCLAIM reclaim + XREADGROUP) → C1 CompositeDeduplicator
        ▼
RecordingShadowSink  (NON-AUTHORITATIVE)
        ▼
shadow_compare.compare(expected_views, applied_views) → ParityReport
```

The producer side runs through the real composition-root helper `build_publication_stack` and the
real `PublishingEventSink` (the exact seam `ProviderSupervisor` drives). The full
`MarketIngestionService` (publisher mode: `INGESTION_SHADOW_PUBLISH`) is exercised for the
provider/supervisor/service-lifecycle tests. Only the D1 seam is faulted (a controllable
`AtomicPublisher` double) in the failure-path tests; M2, L1, and the sink stay real.

## Event counts (§36 large replay — `test_h5_t26_large_replay_ten_thousand_inputs`)

| stage | metric | value |
|-------|--------|-------|
| provider | `provider_generated` (input events) | 10,000 |
| M2 ingress | `ingress_received` (sink.handle) | 10,000 |
| M2 | `m2_accepted` (enqueued) | 10,000 |
| D1 | `d1_published` (boundary published_total) | 10,000 |
| consumer | `consumer_received` (received_total; claimed_total 0) | 10,000 |
| sink | `sink_applied` | 10,000 |
| C1 | `c1_duplicates` | 0 |
| parity | `parity_matched` | 10,000 |
| parity | `parity_failures` (missing+unexpected+mismatch+decode) | 0 |
| parity | `is_clean` | true |

Mix: 5 instruments × 4 IPC-supported kinds (tick, quote, reference, tick+session-OHLC), one
producer incarnation (epoch 1), contiguous producer sequence.

## Semantic parity, not serialization

Parity is by canonical **model value** (`view_from_envelope` decodes the payload; the H4C
comparator compares `Tick`/`Quote`/`MarketReference` model equality — Decimals exact, timestamps
tz-aware). Identity is the dedup key `(producer_id, producer_epoch, producer_sequence)`.
`ORDERING_MODEL = identity_set` (no global order asserted). Expected identities are reconstructed
analytically from the deterministic producer stamping contract, so the comparator proves the
*applied* output matches what the producer *must* have emitted.

## Failure scenarios (all fail-closed / surfaced, never a false clean parity)

| scenario | test | evidence |
|----------|------|----------|
| M2 bounded-queue overflow | T16 | `REJECTED_OVERFLOW` → `PublicationTerminalError`, L1 `BROKEN`, `overflow_total ≥ 1` |
| D1 publish failure | T17 | worker `publish_failure`, L1 `BROKEN` (`PUBLICATION_FAILED`) |
| accepted vs published position | T18 | `published_total == 0` while `last_accepted_sequence == 1` |
| terminal break sticky | T15 | provider disconnect+reconnect does **not** clear `BROKEN` (§25) |
| legal producer-sequence gap | T10 | seq 2 allocated-but-dropped at D1; consumed parity clean, no `MISSING` |
| stranded PEL recovery | T11 | first page stranded in a dead PEL, reclaimed via XAUTOCLAIM → clean, PEL drains to 0 |
| ACK lost after apply+mark | T12 | reclaim recognises the durable mark → single application (`known_b2_duplicate == 0`) |
| B2 apply→mark reapply | T13 | crash before durable mark → reclaim reapplies → `known_b2_duplicate == 1`, not clean |
| malformed stream entry | T20 | envelope decode failure surfaced as `decode_failure_total ≥ 1`, not `MISSING` |
| provider disconnect/recovery | T14 | `PROVIDER_DEGRADED` → reconnect → publication evidence → `HEALTHY`, parity clean |

## Producer identity (M1) & restart

- **Identity preserved** (T05): `producer_id = market-ingestion`, epoch stable within a run,
  sequence progresses 1..N contiguous.
- **Restart → higher epoch** (T06): a second incarnation on the same durable epoch-state directory
  allocates a strictly higher epoch; each epoch's sequence restarts at 1.
- **Same seq / new epoch distinct** (T07): `(epoch=1, seq=1)` and `(epoch=2, seq=1)` are two
  distinct events, never a duplicate.

## Publication kinds

- **STREAM_ONLY** (T03): quote / plain tick write no reference key.
- **STREAM_PLUS_REFERENCE** (T04/T19): `MarketReference` and `Tick.session_ohlc` append the stream
  entry **and** the compacted `md:reference:<date>` hash atomically (both present after publish).

## Timestamp limitation (unchanged from H4C)

Fixtures use canonical tz-aware **UTC** timestamps; **no** `+5:30`/`-5:30` workaround (asserted by
`test_h5_t25_fixture_timestamps_are_canonical_utc`). FIX-2A remains `RC3_CONFIRMED = INCONCLUSIVE`.
H5 makes **no** live-provider timestamp claim:

- `LIVE_DHAN_TIMESTAMP_PARITY = NOT_PROVEN`
- `OFFLINE_CANONICAL_TIMESTAMP_HANDLING = PROVEN`

Live comparison is gated on the FIX-2 track resolving.

## Blocker statuses (unchanged; surfaced, not solved)

- **B2** (apply→mark atomicity) = `DESIGN_RESOLVED_IMPLEMENTATION_PENDING` — H8A. Demonstrated and
  counted here (T13); not solved.
- **B4** (stream retention vs dedup/redelivery horizon) = `DESIGN_RESOLVED_IMPLEMENTATION_PENDING`
  — H8B. Offline replay proves nothing about production retention.
- **B11** (Redis durability / stream-loss detection) = `DESIGN_RESOLVED_IMPLEMENTATION_PENDING` —
  H8C. Parity over known fixtures is not stream-loss detection.

## Reference bootstrap boundary (§27)

H5 offline E2E parity requires only **stream** events; the consumer reads `md:events` and applies
every stream entry (reference-bearing events included). Backend **reference bootstrap** (loading
the compacted `md:reference:<date>` hash into a starting consumer) is **out of H5 scope** and
remains **NOT PROVEN** — H5 implements no new authority/bootstrap contract. Producer-side reference
atomicity (stream + reference written together) is verified (T04/T19); consumer bootstrap is not.

## Cutover-evidence matrix (§29)

| property | status | owner |
|----------|--------|-------|
| producer identity durability (M1) | PROVEN | H5 (T05/T06/T07) |
| async publication boundary (M2) | PROVEN | H5 (T16, overflow fail-closed) |
| atomic stream/reference publish (D1) | PROVEN | H5 (T04/T19) |
| feed continuity state machine (L1) | PROVEN | H5 (T14/T15/T17/T18) |
| consumer durable dedup (C1) | PROVEN | H5 (T08/T09/T12) |
| abandoned PEL recovery (H4B) | PROVEN | H5 (T11) |
| offline semantic parity | PROVEN | H5 (T01/T02/T26/T27) |
| backend reference bootstrap | NOT PROVEN — out of scope | H-later |
| backend restart, live topology | NOT YET | H6 |
| ingestion restart, cutover topology | NOT YET | H7 |
| B2 authority safety | NOT YET | H8A |
| retention / dedup horizon | NOT YET | H8B |
| Redis durability / loss detection | NOT YET | H8C |
| live Dhan timestamp parity | NOT YET | FIX-2 |
| single-owner runtime interlock | NOT YET | H9A |
| live single-owner transfer | NOT YET | H9B |
| IPC authority verification | NOT YET | H9C |

## Required test matrix (§35)

| id | test | topology |
|----|------|----------|
| T01 | baseline end-to-end canonical parity | full service |
| T02 | all supported canonical kinds | full service |
| T03 | STREAM_ONLY publication | real stack |
| T04/T19 | STREAM_PLUS_REFERENCE + reference atomicity | real stack |
| T05 | M1 identity preserved | real stack |
| T06/T07 | ingestion restart → new epoch; same seq/new epoch distinct | real stack |
| T08 | backend consumer restart → durable dedup persists | consumer + durable C1 |
| T09 | duplicates suppressed | consumer + at-least-once redelivery |
| T10 | legal sequence gap tolerated | real stack + D1 fault |
| T11 | PEL stranded/recovered | consumer (XAUTOCLAIM) |
| T12 | ACK lost → no reapply | consumer + ACK-crash |
| T13 | B2 duplicate surfaced | consumer + record-crash |
| T14 | provider disconnect/recovery | full service |
| T15/T25§ | terminal publication failure sticky | real M2/L1 + D1 fault |
| T16 | M2 queue overflow visible / fail-closed | real M2 + D1 stall |
| T17/T18 | D1 publish failure visible; accepted ≠ published | real M2/L1 + D1 fault |
| T20 | malformed stream evidence visible | consumer |
| T21/T22/T24 | no TickEngine/MarketContext/Dhan; shadow sink only | full service + arch test |
| T23 | no strategies/session/trading path | arch import test |
| T25 | no FIX-2 workaround (canonical UTC) | fixture assertion |
| T26 | large replay ≥ 10,000 inputs | full service |
| T27/§37 | deterministic reruns (×3, identical verdict) | full service |
| T28 | bounded diagnostics / comparator sample | consumer |
| T29 | H4 subsystem remains green | full-suite regression |
| T30 | H3 publication subsystem remains green | full-suite regression |

T21–T24 are additionally proven structurally by
`tests/architecture/test_market_ipc_import_boundary.py::test_h5_offline_topology_touches_no_authority_broker_or_fix_surface`
(the H5 topology imports no authority/broker/FIX surface). T29/T30 are regression gates satisfied by
the H3/H4 subsystem suites and the full suite.

## Cutover prerequisites (not satisfied by H5)

H5 PASS means `READY_FOR_H6 = YES` only. `READY_FOR_LIVE_CUTOVER = NO` and
`READY_FOR_IPC_AUTHORITY = NO`: backend restart under a live topology (H6), ingestion-restart
cutover (H7), B2/B4/B11 (H8), single-owner interlock/transfer/authority verification (H9), and live
Dhan timestamp parity (FIX-2) all remain open. `SINGLE_DHAN_OWNER = TRUE` is trivially preserved (no
real Dhan; no second account introduced).
