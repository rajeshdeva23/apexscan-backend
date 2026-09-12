# ADR-024 — Broker-Neutral Market Feed Continuity Runtime Contract

| Field | Value |
|-------|-------|
| **Status** | Proposed |
| **Date** | 2026-09-13 |
| **Deciders** | Platform / Market-Ingestion Decoupling Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | ADR-006 (feed continuity fact), ADR-020 (M1 epoch), ADR-021 (D1 atomic publish), ADR-022 (M2 async boundary), ADR-023 (C1 durable dedup); DECOUPLING-L1 |

---

## Context

The existing `FeedContinuity` / `FeedContinuityEvent` (ADR-006) is a **market-data-quality**
fact — provider connectivity — consumed by the in-process candle engine to invalidate candle
completeness. It says nothing about whether canonical events actually reached the decoupled IPC
transport. As M1/D1/M2/C1 built the decoupled producer path, there was no runtime notion of
**canonical publication continuity**: whether the producer's canonical events were accepted (M2)
and completed (D1), and whether that stream of events is unbroken for the current producer
incarnation.

## Problem (L1)

Wire a broker-neutral feed-continuity runtime over the decoupled producer path so that
publication failures are **explicitly observable** and a future Phase-H authority can fail
closed — without activating IPC/M2/C1, without changing M1/D1/M2/C1 semantics, and without
touching the in-process authoritative continuity path.

## Decision

Add `app/market_ipc/continuity.py`: `FeedContinuityTracker` — a purely observational, in-memory,
O(1) state machine (`ContinuityState` + `ContinuityReason` + bounded `FeedContinuitySnapshot`).
It is a distinct, non-competing concern from ADR-006's candle-completeness fact.

### Four independent dimensions (never collapsed)
- **A. provider connectivity** — socket up/down (`provider_connected` bit).
- **B. canonical producer progression** — M1 `(producer_id, producer_epoch)` incarnation + the
  canonical `producer_sequence`.
- **C. publication acceptance** — M2 accepted the event into the bounded queue (`last_accepted_
  sequence`). **Acceptance is NOT Redis durability.**
- **D. publication completion** — D1 confirmed the Redis publish (`last_published_sequence`).

### State model
`NOT_STARTED → HEALTHY`; `HEALTHY ⇄ PROVIDER_DEGRADED` (recoverable); any terminal break →
`BROKEN` (sticky for the incarnation); `STOPPING → STOPPED`. Every state carries a
machine-readable `ContinuityReason` (never a bare boolean).

### Explicit evidence beats gap inference
Continuity breaks only on **explicit** signals — queue overflow, worker fault, publication
failure/uncertainty, provider disconnect, incomplete drain. A `producer_sequence` gap
(e.g. `100,101,103,104` because `102` was legally overflow-rejected — M2 allocates identity
before enqueue, ADR-022) is **never** treated as provider packet loss.

### Terminal-break stickiness + incarnation reset
Overflow, worker failure, definite publication failure, and unknown publication outcome are
**terminal for the producer incarnation**. A provider reconnect never clears them (a reconnect
without a successful publication only moves `PROVIDER_DEGRADED` to `AWAITING_RECOVERY_EVIDENCE`;
a subsequent `publication_succeeded` is the recovery evidence). A terminal break is cleared only
by a **new incarnation** (`producer_started` with a new `producer_epoch`); cumulative counters
are retained (bounded ints) for observability across incarnations.

### Wiring seams (non-invasive)
- `record_submission(SubmitOutcome, producer_sequence)` at the M2 submit boundary.
- `record_publication(PublishOutcome, producer_sequence)` at the D1 transmit boundary.
- `observe_boundary(BoundaryDiagnostics)` derives worker-failure / overflow / publish-failure /
  recovery from the boundary's **public** diagnostics via idempotent counter deltas — so a
  composition can drive continuity **without modifying M2**.

### Wiring requirements for a future Phase-H composition
- `observe_boundary` (or equivalent diagnostics polling) is **mandatory** for worker-fault
  detection. When the M2 worker faults the boundary goes `FAILED`; `submit` then returns
  `REJECTED_NOT_RUNNING` and `transmit` is no longer called, so the `record_submission` /
  `record_publication` seams alone **cannot** observe the fault — continuity would stay `HEALTHY`
  forever. A composition wiring only the submit/publication seams is incomplete.
- One tracker instance must be driven by **one boundary per producer incarnation**. A new
  incarnation (`producer_started` with a new `producer_epoch`) resets this tracker's seen-counter
  baselines to zero, which is correct only against a **fresh** boundary whose cumulative counters
  also restart at zero (the documented M2 lifecycle: new epoch = new `publisher.start()` = new
  boundary). Reusing one boundary across an epoch change would replay its historical
  overflow/failure counters as fresh terminal breaks against the new incarnation.

### Rejected alternatives
- **Reusing ADR-006's `FeedContinuity` enum** — it is a candle-completeness market-data fact
  consumed by the engine; it cannot express queue overflow, worker failure, or accepted-vs-
  published positions. Overloading it would collapse dimension A into C/D.
- **Inferring provider loss from sequence gaps** — forbidden (§6/§7): gaps are legal.
- **Clearing terminal breaks on provider reconnect** — hides an unresolved canonical hole.
- **Adding an observer hook inside `AsyncPublicationBoundary`** — would change M2 semantics;
  `observe_boundary` reads the existing public diagnostics instead.

## Failure semantics

- **Queue overflow** (`REJECTED_OVERFLOW`) → terminal `PUBLICATION_QUEUE_OVERFLOW`; a later
  success never auto-clears it.
- **Worker fault** (`BoundaryState.FAILED`) → terminal `PUBLICATION_WORKER_FAILED`; no silent
  self-recovery.
- **Definite publication failure** → terminal `PUBLICATION_FAILED`; the published position does
  not advance.
- **Unknown outcome** → terminal `PUBLICATION_OUTCOME_UNCERTAIN`; never claims loss or success.
  The current D1/publisher contract returns `FAILED_TRANSPORT`, which conflates a definite
  failure with an unknown outcome; `record_publication` maps it **conservatively** to
  `publication_failed` rather than fabricating certainty. Distinguishing them is deferred to the
  publisher contract, not expanded here.
- **Prepare rejection** (unsupported/oversize/serialization) → counted, **non-terminal** (a
  data-validity rejection of a malformed event, not a transport continuity break).
- **Incomplete drain** → `STOPPED` with `INCOMPLETE_DRAIN` + `pending_at_stop`; a clean drain
  after a prior break keeps the break reason (never reports clean-stopped over an unresolved
  break).

## Performance

All observation methods are O(1), in-memory, non-blocking, and perform **no** Redis / filesystem
/ database / network I/O (they mutate fixed fields). The snapshot is a bounded frozen dataclass
with no per-instrument cardinality and no unbounded history. Continuity mutation, ingestion, and
the M2 publication worker share one asyncio event loop (no cross-thread state).

## Explicit non-guarantees

- L1 **does not enable IPC** (nothing composes the tracker into the production/default runtime).
- L1 **does not provide exactly-once** and **does not replace C1** (it observes positions and
  delivery status; it performs no dedup).
- L1 **does not resolve C1's authoritative apply→mark window** — Phase H owns that.
- L1 **does not start Phase H** and **does not touch FIX-2** (no timestamp / TickEngine / Dhan
  changes).

## Consequences

- **Phase H** — unblocked for *design*: continuity is now a truthful, fail-closed signal a
  Phase-H authority gate can consult (requiring, e.g., `state == HEALTHY` **and**
  `provider_connected`). Activation remains out of scope.
- **M1/D1/M2/C1** — semantics unchanged; the tracker only observes their public outputs.
