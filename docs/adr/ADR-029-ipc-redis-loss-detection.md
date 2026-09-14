# ADR-029 — IPC Redis Loss / Continuity Detection (B11)

| Field | Value |
|-------|-------|
| **Status** | Proposed (implementation on `feature/decoupling-hardening`; not activated — IPC stays off) |
| **Date** | 2026-09-15 |
| **Deciders** | Platform / Market-Ingestion Decoupling Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | ADR-024 (L1 continuity), ADR-025 (Phase-H design; B11 + fail-closed authority gate), ADR-023 (C1), ADR-028 (B4 retention); Phase H8C |

> This ADR records the B11 closure implemented in H8C. It **reuses** ADR-025's frozen "consume-side
> loss detector reconciled against producer L1" and introduces **no new persistent authority state**
> — the detector is stateless and reads only Redis-native metadata. It activates nothing (Phase H9
> owns authority). Status stays **Proposed** pending governance acceptance, a precondition for
> authoritative activation.

## Context (the B11 gap)

Producer-side L1 confirms delivery at D1's `XADD` ack and therefore **cannot** observe a subsequent
Redis loss: an AOF `everysec` tail-loss (up to ~1 s of confirmed writes), a `FLUSHALL`/reset, or a
restore of an older snapshot. Authority readiness (ADR-025) requires distinguishing a real loss from
the benign states — consumer lag, pending recovery, legitimate H8B trimming, a producer publication
failure, a legal producer-sequence gap, and a new producer epoch — **without** treating a
producer-sequence gap as proof of loss.

## Decision

Add a **stateless** consume-side detector (`app/market_ipc/loss_detection.py`) that reconciles three
point-in-time evidence snapshots and classifies transport continuity. It persists nothing.

**Evidence sources**
- **Producer (L1, survives Redis loss).** `FeedContinuitySnapshot` → `ProducerPublicationEvidence`:
  `(producer_id, producer_epoch, last_published_sequence, terminal_break, outcome_uncertain)`. Uses
  the **published** (D1-confirmed) position, never accepted/allocated. L1's epoch file lives on the
  ingestion host, not Redis, so this evidence survives a Redis reset.
- **Redis (bounded, native).** `XINFO STREAM` (length, `last-generated-id`), `XINFO GROUPS`
  (`last-delivered-id`, `pending`), and one `XREVRANGE COUNT 1` decoded to the last entry's canonical
  identity. O(1) — no unbounded stream scan. Uses only Redis 6.2-available fields; the richer
  `entries-added`/`entries-read`/`lag` (Redis ≥ 7.0) are **not** read.
- **Consumer.** the last canonical identity the backend durably applied.

**Classification** (`LossDetectionState`): `HEALTHY`, `CONSUMER_LAGGING`, `PENDING_RECOVERY`,
`RETENTION_EXPECTED`, `PRODUCER_PUBLICATION_FAILED`, `REDIS_STREAM_RESET`, `REDIS_STATE_REWIND`,
`PUBLISHED_EVENT_UNACCOUNTED_FOR`, `INSUFFICIENT_EVIDENCE`. Reconciliation order: producer-cause
first (a broken/uncertain producer is never a Redis loss), then reset/rewind, then producer↔stream
tail reconciliation, then consumer lag/pending.

**Detection signals** (Redis 6.2-compatible):
- **RESET** — the producer has published but the stream is absent / `last-generated-id == 0-0` / the
  consumer group is gone.
- **REWIND** — the group's durable `last-delivered-id` sorts strictly after the stream's
  `last-generated-id` (Redis holds fewer entries than the group already consumed).
- **PUBLISHED_EVENT_UNACCOUNTED_FOR** — the last stream entry's sequence is behind the producer's
  confirmed `last_published` (a tail-loss), or the stream retains nothing yet the events were never
  applied.
- **RETENTION_EXPECTED** — the stream trimmed everything but the consumer already applied up to the
  producer's published position (the H8B applied-then-aged-out case).

**Hard rules**
- Never infer loss from producer-sequence arithmetic; reconcile the **published** position against
  the last stream entry, never "sequence N+1 is missing".
- Scope every decision to one `(producer_id, producer_epoch)`; a new epoch is never a rewind.
- **Fail closed:** unavailable metadata, an uncertain producer outcome, or any unresolved
  reset/rewind/unaccounted-publication → `ready_for_authority = False`.

## Why no new persistent authority (no STOP)

Detecting reset/rewind here does **not** require a new durable checkpoint / stream manifest /
cross-process protocol: the producer's live L1 is the durable reference (survives a Redis loss), and
Redis-native monotonic signals (`last-generated-id`, group `last-delivered-id`) provide the observed
state. Because no new persistent authority is introduced, §54's STOP does not trigger; this is the
implementation of ADR-025's already-frozen detector concept.

## Authority gate

`ready_for_authority` is a **readiness input** to ADR-025's fail-closed authority gate (C1 ∧ L1 ∧
consumer ∧ backlog ∧ sink). H8C activates nothing; a non-ready classification simply keeps IPC
authority unavailable.

## Consequences

- **B11 → RESOLVED**: Redis reset/rewind and confirmed-publication tail-loss are detected and fail
  closed; consumer lag / pending / legitimate trimming / producer failure / legal gaps / new epochs
  are correctly distinguished; no sequence arithmetic; bounded Redis reads. B2 (H8A) and B4 (H8B)
  unaffected.
- Adds `app/market_ipc/loss_detection.py` only; no producer/consumer change, no authority, no
  activation. **Acceptance of this ADR is a precondition for authoritative activation (H9/H8D).**

## Limitations

- **Producer-evidence conveyance** to the backend (via the frozen `md:health` snapshot) is wired at
  activation (H9), not H8C; the detector takes the producer snapshot as an input here.
- **AOF tail-loss of *un-consumed* events** is caught only via the producer↔stream last-entry
  reconciliation (published position vs. last stream identity); it depends on the producer L1 record
  being available to the detector.
- **`REDIS_STATE_REWIND`** specifically flags the group's delivered position sorting past the
  stream's last id. A *consistent* older-snapshot restore (stream **and** group roll back together,
  so group ≤ stream) is instead caught by the tail-loss branch (`PUBLISHED_EVENT_UNACCOUNTED_FOR`) —
  either way `ready_for_authority` is False; only the label differs.
- **Non-suffix loss** (an arbitrary middle entry deleted while newer entries and `last-generated-id`
  survive) is outside the threat model (tail-loss / `FLUSHALL` / snapshot-restore are all
  contiguous-suffix or total) and is consistent with the no-sequence-arithmetic rule; it is not
  detected.
- Redis ≥ 7.0 `entries-added`/`entries-read`/`lag` are not read; they would add redundancy only.
