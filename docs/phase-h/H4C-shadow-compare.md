# Phase-H4C — Offline IPC Shadow Comparison & Replay Framework (Offline / Test-Redis Only)

Governed by **ADR-023** (durable consumer idempotency) and **ADR-025** (activation flag matrix).
H4C adds a deterministic, offline framework that feeds canonical fixture events through the
Phase-A/B publish path and a real (disposable) Redis stream, consumes them through the H4A/H4B
shadow consumer, captures the non-authoritative applied output, and compares it against the
expected fixtures — classifying every logical event and surfacing anomalies. No new architecture,
no new persistent format, no new config, no ADR.

> **H4C DOES NOT USE REAL DHAN. H4C DOES NOT CONTACT PRODUCTION. H4C DOES NOT ACTIVATE THE
> CONSUMER IN PRODUCTION. H4C DOES NOT MAKE IPC AUTHORITATIVE. H4C DOES NOT DRIVE THE TICKENGINE
> OR MARKETCONTEXT. H4C DOES NOT SOLVE B2 / B4 / B11. H4C DOES NOT IMPLEMENT FIX-2 OR TOUCH
> FIX-2A PR #63. H4C DOES NOT START H4D. H4C IS NOT LIVE PARITY VALIDATION.**

## Topology

```
canonical fixture events (deterministic, tz-aware UTC)
        │  build_envelope (Phase-A envelope + producer identity)
        ▼
RedisMarketEventStream.publish → md:events   (real disposable redislite, private unix socket)
        ▼
MarketEventConsumer (H4A poll_once: XAUTOCLAIM reclaim pass + XREADGROUP new pass)
        │  → gate → C1 CompositeDeduplicator (durable Redis + memory) → decode
        ▼
RecordingShadowSink   (NON-AUTHORITATIVE: records (envelope, payload) only)
        ▼
shadow_compare.compare(expected_views, applied_views) → ParityReport
```

The comparator is a pure function; it holds no Redis client, spawns no task, and imports no
provider/authority surface (proven by `test_shadow_compare_imports_no_authority_redis_or_provider_surface`).

## Comparison contract

`app/market_ipc/shadow_compare.py::compare` classifies each logical event into a stable
`ParityClass`:

| Class | Meaning | Anomaly? |
|-------|---------|----------|
| `MATCH` | expected identity applied once with equal canonical value | no |
| `DUPLICATE_SUPPRESSED` | fixture repeated an identity; C1 applied it once | no (healthy C1) |
| `VALUE_MISMATCH` | expected identity applied, but a field differs | yes |
| `MISSING` | expected identity never applied | yes |
| `UNEXPECTED` | applied identity absent from the fixture | yes |
| `KNOWN_B2_DUPLICATE` | an identity applied more than once (apply→mark reapply) | yes (see B2) |
| `DECODE_FAILURE` | poison entry counted from consumer diagnostics | yes |
| `UNSUPPORTED` | unsupported-schema count from consumer diagnostics | yes |

`ParityReport` carries bounded scalar totals plus a bounded `sample` (capped by `sample_limit`,
default 50) of `ParityMismatch` records — each with the identity string, kind, and, for a value
mismatch, the first differing `field` / `expected` / `actual` (each value truncated). `is_clean`
is true only when **every** anomaly total is zero; `DUPLICATE_SUPPRESSED` and `MATCH` are healthy
and never break cleanliness. `ParityReport` and `ParityMismatch` are frozen pydantic models
(JSON round-trippable, diagnostic-safe).

## Comparison is semantic, not byte-level

Events are compared by canonical **model equality** (`Tick`/`Quote`/`MarketReference`/
`FeedContinuityEvent` are frozen, strict pydantic models: Decimals compared exactly, timestamps
tz-aware). Field diffs come from `model_dump`, never from raw JSON bytes — so serialization or
formatting noise can never masquerade as semantic drift. Decimal/integer money fields are compared
exactly (no epsilon); no floating tolerance is introduced.

## Event identity

The primary comparison key is the existing dedup identity
`(producer_id, producer_epoch, producer_sequence)` — never a competing scheme. Domain identity
(instrument, event kind) is compared as part of the canonical payload value. A new producer epoch
reusing a sequence is a **distinct** event, never a duplicate.

## Ordering guarantees / non-guarantees

`ORDERING_MODEL = "identity_set"`. Parity is **identity-set / event-semantic sensitive**, never
order-sensitive. H4B established that reclaimed (PEL) and newly-read entries have no global
application order; the comparator therefore keys on identity and ignores order, so reclaim/new
interleaving never produces a false `VALUE_MISMATCH`/`MISSING`/`UNEXPECTED`. Global order is
**not** asserted anywhere.

## No loss inference from sequence gaps

The comparator uses producer sequences **only** as part of the opaque identity key — never for
arithmetic. Only events explicitly present in the fixture are "expected", so a legal producer
sequence gap (a sequence allocated before enqueue then dropped on bounded-queue overflow) is
never reported as `MISSING`. Missing is reported solely when an explicitly-expected identity was
not applied.

## Duplicate semantics (C1)

C1 suppresses reapplication of a durable duplicate, so a fixture that repeats an identity
correctly yields `input count > apply count`. The comparator treats the extra fixture occurrences
of an applied identity as `DUPLICATE_SUPPRESSED` (healthy), not `MISSING`. This is proven both as
a pure classification and through a real redislite replay.

## Pending-recovery interaction

Fixture events stranded in a dead consumer's PEL and then reclaimed via XAUTOCLAIM reach full
parity once recovery completes (`test_pending_recovery_reaches_parity`). An ACK-lost entry, once
reclaimed, is recognised as a durable duplicate and **not** reapplied, so the comparator sees a
single logical application (`known_b2_duplicate_total == 0`).

## B2 classification (surfaced, never solved)

The apply→mark window (**B2**, owned by H8A) can reapply an event when the sink applied but the
durable mark never committed before a crash/reclaim. The comparator surfaces this distinctly as
`KNOWN_B2_DUPLICATE` (an identity applied more than once) and it breaks `is_clean`. H4C
**demonstrates and counts** the B2 window through a real crash/reclaim replay; it does not hide it
and does not attempt to eliminate it. B2 remains `DESIGN_RESOLVED_IMPLEMENTATION_PENDING`.

## Malformed / poison entries

Permanently-invalid entries carry no recoverable identity; the H4A consumer counts them and
terminally ACKs them (they never reach the sink). The comparator surfaces them as an aggregate
`decode_failure_total` (sourced from `ConsumerDiagnostics`), so a poison entry is visible in the
parity evidence and is never silently dropped nor mistaken for `MISSING`.

## FIX-2 limitation

FIX-2A is `RC3_CONFIRMED = INCONCLUSIVE`. H4C therefore uses **already-correct tz-aware UTC
fixture timestamps** and encodes **no** `+5:30`/`-5:30` workaround (asserted by
`test_fixture_timestamps_are_canonical_utc`). H4C makes no live-provider timestamp claim:

- `LIVE_PROVIDER_TIMESTAMP_PARITY = NOT_PROVEN`
- `LIVE_TIMESTAMP_PARITY_READY = NO`
- `READY_FOR_LIVE_COMPARE = NO`

Live comparison is gated on the FIX-2 track resolving.

## Blocker statuses (unchanged)

- **B2** (apply→mark atomicity) = `DESIGN_RESOLVED_IMPLEMENTATION_PENDING` — H8A. Made observable
  by the comparator here, not solved.
- **B4** (stream retention vs dedup/redelivery horizon) = `DESIGN_RESOLVED_IMPLEMENTATION_PENDING`
  — H8B. Offline replay proves nothing about production retention.
- **B11** (Redis durability / stream-loss detection) = `DESIGN_RESOLVED_IMPLEMENTATION_PENDING` —
  H8C. Parity over known fixtures is not stream-loss detection.

## H4D boundary

H4D reviews consumer/recovery/comparison readiness as a whole. H4C implements no live activation,
no authority, and does not begin H4D.
