# Phase-H4D — Backend IPC Consumer Readiness Review (H4A + H4B + H4C Integrated Gate)

Governed by **ADR-023** (durable consumer idempotency) and **ADR-025** (activation flag matrix).
H4D is a **review / evidence-consolidation** phase: it validates that the non-authoritative
backend IPC consumer subsystem (H4A composition + durable C1, H4B pending recovery, H4C offline
parity) is internally coherent, failure-safe, replay-verifiable, and ready to become an input to
H5 offline parity / cutover-evidence preparation. No new architecture, no new config, no ADR.

> **H4D DOES NOT USE REAL DHAN. H4D DOES NOT CONTACT PRODUCTION. H4D DOES NOT ACTIVATE THE
> CONSUMER. H4D DOES NOT MAKE IPC AUTHORITATIVE. H4D DOES NOT DRIVE THE TICKENGINE OR
> MARKETCONTEXT. H4D DOES NOT SOLVE B2 / B4 / B11. H4D DOES NOT IMPLEMENT FIX-2 OR TOUCH FIX-2A
> PR #63. H4D DOES NOT START H5.**

## Reviewed baseline

- Production `main` = `a6b8c68ddd87e5d2a00c19485a2bf116285641fe` — UNCHANGED.
- Holiday `feature/decoupling-hardening` = `13692e917e7e5aaebeb560cff9a15967aff60be9` (H4C head).
- FIX-2A PR #63 = OPEN / DRAFT / base `main` / UNMERGED — untouched; `RC3_CONFIRMED = INCONCLUSIVE`.
- H4A = PASS, H4B = PASS, H4C = PASS.

H4D adds **evidence only** (one integrated integration test + this document); no production code
changed.

## Integrated topology (validated end to end)

```
deterministic canonical fixture (tz-aware UTC)
        │  build_envelope → Phase-A envelope + producer identity
        ▼
RedisMarketEventStream.publish → md:events   (real disposable redislite, private unix socket)
        ▼
MarketEventConsumer.poll_once
    ├─ XAUTOCLAIM bounded reclaim pass (H4B)  ─┐
    └─ XREADGROUP new pass (H4A)              ─┤ both route through the SAME _handle path
        ▼                                      │
    envelope decode → trading-date/universe/kind gates
        ▼
    durable C1 CompositeDeduplicator (Redis authority + memory hot cache)
        ▼
    RecordingShadowSink  (NON-AUTHORITATIVE: records (envelope, payload) only)
        ▼
    shadow_compare.compare(expected, applied) → ParityReport
```

There is **no alternate path** that bypasses C1, the shared `_handle` processing, or the
non-authoritative sink: both the reclaim and the new-read passes call `_handle` (`consumer.py`),
and the only sink connected anywhere in H4 is `RecordingShadowSink`. Verified by the architecture
import-boundary tests and the integrated scenario.

## ACK contract (verified end to end, `consumer.py`)

| Case | Path | ACK? |
|------|------|------|
| **New** | durable `contains`=false → `sink.apply` → durable `record` → `XACK` | yes (terminal) |
| **Duplicate** | durable `contains`=true → no reapply → `XACK` | yes (terminal) |
| **Permanent poison** | undecodable → counted → `XACK` | yes (terminal, cannot jam the group) |
| **Transient (sink/dedup/ack fail)** | not in `_TERMINAL_ACK_OUTCOMES` | **no** (stays pending, redelivers) |

No path ACKs before durable correctness: the `XACK` in `_finalize` runs only after `sink.apply`
and the durable `record` both succeed (new events) or the durable duplicate is confirmed.

## C1 authority (`durable_dedup.py`)

- `DurableDeduplicator` (Redis `EXISTS` / `SET ex=dedup_ttl_seconds`) is the **sole** correctness
  authority; a Redis failure **propagates** so the caller fails closed (entry left pending, never
  applied blind).
- `CompositeDeduplicator` memory (`BoundedDeduplicator`) is a **hot cache only**: an entry appears
  in memory only after a durable `record` (durable-first) or after `contains` warms it from a
  durable hit. Memory can therefore never suppress an application that was not first durably
  recorded/confirmed. A fresh process (empty cache) still sees a prior identity via the durable
  `contains` (proven by the H4A "duplicate survives runtime restart" test).

## Restart correctness

| Scenario | Expected | Evidence |
|----------|----------|----------|
| Restart after durable mark | no reapply | H4A runtime-restart dedup test; H4B ACK-lost test |
| Restart with pending unprocessed entry | XAUTOCLAIM eventually reclaims | H4B abandoned/multi-page tests |
| Restart after ACK loss | durable duplicate → no reapply → ACK | H4B + H4D ACK-loss tests |
| Restart inside apply→mark window | possible reapplication (**B2**) | H4B + H4D B2 tests |

## Pending recovery (H4B)

Bounded XAUTOCLAIM reclaim (one `read_count` page/cycle, `claim_idle_ms` idle threshold) then a
new-read pass, every cycle. Abandoned entries are reclaimed after idle; fresh/below-idle entries
are protected; a backlog larger than one page drains across successive cycles; recovery and new
traffic each make progress in the same cycle (no starvation). The cursor restarts at `"0-0"` each
pass and relies on XAUTOCLAIM idle-resetting reclaimed entries so the next scan advances — proven
non-pathological by the H4B multi-page test (250 entries, ≤100/cycle, ≥3 non-empty cycles, PEL
drains) and the H4D integrated healthy scenario (stranded page + fresh entries drained by one
consumer). `RedisMarketEventStream.claim_page_raw` indexes the XAUTOCLAIM response positionally,
tolerating the Redis 6.2 2-tuple and ≥7.0 3-tuple (the deleted-IDs element is ignored — Redis has
already removed those from the PEL).

## Comparator semantics (H4C, `shadow_compare.py`)

`compare` classifies each logical event by dedup identity into `match` / `value_mismatch` /
`missing` / `unexpected` / `duplicate_suppressed` / `decode_failure` / `unsupported` /
`known_b2_duplicate`. Comparison is by canonical **model equality** and `model_dump` field diff
(never JSON bytes); totals are scalars and the mismatch sample is capped by `sample_limit`. C1
duplicate suppression is `duplicate_suppressed` (healthy), never MISSING/UNEXPECTED; a
double-application is `known_b2_duplicate` and breaks `is_clean`.

## Ordering (non-guarantee)

`ORDERING_MODEL = "identity_set"`. Parity is identity-keyed and order-INSENSITIVE. H4B guarantees
**no** global application order across reclaimed and newly-read entries; no H4 test or document
reintroduces a global-order guarantee.

## Legal sequence gaps & epoch identity

No component uses producer-sequence arithmetic to infer loss: `N`, `N+2` is a legal M2 gap, never
"missing". Identity is `(producer_id, producer_epoch, producer_sequence)`; epoch 1 / seq 1 is
distinct from epoch 2 / seq 1 (proven in H4B and H4C).

## Multi-event-kind support

The consumer and comparator handle every current `EventKind`: `Tick` (incl. `session_ohlc`),
`Quote`, `MarketReference`, `FeedContinuityEvent`. No unsupported kind is claimed.

## Limitations (explicit, not overclaimed)

- **Reference bootstrap** — H4A–H4C validate **stream** consumption + shadow parity only. Backend
  reference-state bootstrap from `md:reference:<trading_date>` and authority reconstruction are
  **NOT proven here** (Phase-D machinery exists but is not part of the consumer read path under
  review).
- **Timestamps** — `OFFLINE_CANONICAL_TIMESTAMP_HANDLING = PROVEN` (tz-aware UTC fixtures,
  round-tripped); `LIVE_DHAN_TIMESTAMP_INTERPRETATION = NOT_PROVEN`;
  `LIVE_TIMESTAMP_PARITY_READY = NO`. No `-5:30`/`+5:30` logic appears anywhere.
- **B2** (apply→mark atomicity) = `DESIGN_RESOLVED_IMPLEMENTATION_PENDING` — H8A. Demonstrated +
  comparator-surfaced, not solved. Harmless only because the sink is non-authoritative.
- **B4** (dedup TTL vs stream retention vs redelivery horizon) =
  `DESIGN_RESOLVED_IMPLEMENTATION_PENDING` — H8B. Local redislite behaviour proves nothing about
  production retention; note the memory cache carries no independent TTL (benign — it only makes
  suppression of an already-applied identity more aggressive, which is correct; the real horizon
  risk is a fresh-process redelivery after the durable key's TTL, owned by H8B).
- **B11** (Redis durability / stream-loss detection) = `DESIGN_RESOLVED_IMPLEMENTATION_PENDING` —
  H8C. Known-fixture parity is **not** loss detection; no sequence-gap detector substitutes for it.

## Evidence matrix

| Requirement | Phase | Test(s) | Code symbol | Result | Residual |
|-------------|-------|---------|-------------|--------|----------|
| Consumer lifecycle / readiness / teardown | H4A | `test_market_ipc_consumer_runtime*` | `MarketEventConsumerRuntime` | PASS | — |
| Durable dedup authority | H4A/C1 | runtime + recovery redis tests | `CompositeDeduplicator`/`DurableDeduplicator` | PASS | B4 horizon |
| Process-restart dedup | H4A | `test_..._runtime_redis` (dup survives restart) | `CompositeDeduplicator.contains` | PASS | — |
| Pending recovery / XAUTOCLAIM | H4B | `test_market_ipc_consumer_recovery_redis` | `poll_once`/`_claim_stale_bounded` | PASS | — |
| Abandoned-consumer reclaim | H4B | `test_consumer_b_reclaims_dead_consumer_a`, `..._two_consumers_same_group` | `claim_page_raw` | PASS | — |
| Below-idle protection | H4B | `test_below_idle_entry_not_stolen` | `claim_idle_ms` | PASS | — |
| ACK-loss single application | H4B/H4D | `test_ack_lost_*`, `test_integrated_ack_loss_subset_*` | durable `contains` | PASS | — |
| Transient-failure retry (no drop) | H4B | `test_sink_failed_pending_retries_on_recovery` | `_TERMINAL_ACK_OUTCOMES` | PASS | — |
| B2 apply→mark exposure | H4B/H4C/H4D | `test_apply_then_mark_crash_*`, `test_b2_*`, `test_integrated_b2_*` | `known_b2_duplicate` | PASS (surfaced) | B2=H8A |
| Legal sequence gaps tolerated | H4B/H4C | `test_legal_sequence_gaps_*` | (no seq arithmetic) | PASS | — |
| Epoch-aware identity | H4B/H4C | `test_same_sequence_new_epoch_*` | `ProducerEventIdentity` | PASS | — |
| Semantic parity match | H4C | `test_..._shadow_compare*` | `compare` | PASS | — |
| Missing / unexpected / mismatch | H4C | shadow-compare unit tests | `compare` | PASS | — |
| Duplicate suppression understood | H4C/H4D | `test_duplicate_*`, integrated healthy | `duplicate_suppressed` | PASS | — |
| Bounded diagnostics | H4C | `test_mismatch_sample_is_bounded` | `sample_limit`/`ConsumerDiagnostics` | PASS | — |
| Offline timestamp handling | H4C | `test_fixture_timestamps_are_canonical_utc` | fixtures | PASS | live=NOT_PROVEN |
| Non-authoritative isolation | H4A–D | architecture import-boundary tests | `test_market_ipc_import_boundary` | PASS | — |
| Integrated coexistence | H4D | `test_market_ipc_h4d_integrated_redis` | full stack | PASS | — |

## Readiness (kept separate)

- `OFFLINE_CONSUMER_SUBSYSTEM_READY = YES`
- `LIVE_CONSUMER_READY = NO`
- `IPC_AUTHORITY_READY = NO`

## H5 entrance criteria (met)

1. H4A/H4B/H4C remain green. ✓
2. No correctness-affecting HIGH/MEDIUM review findings remain. ✓
3. Consumer remains non-authoritative. ✓
4. C1 semantics intact (durable authority, memory cache-only). ✓
5. Pending recovery works. ✓
6. Comparator classifies parity/drift correctly. ✓
7. B2 explicit, not misrepresented as solved. ✓
8. FIX-2 limitation explicit. ✓
9. No production/live dependency required for H5 offline work. ✓

`READY_FOR_H5 = YES` — which does **not** imply `READY_FOR_LIVE_CONSUMER` or
`READY_FOR_IPC_AUTHORITY` (both remain NO). H5 owns offline parity / cutover-evidence preparation.
