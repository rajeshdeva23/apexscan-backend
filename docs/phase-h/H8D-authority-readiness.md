# H8D — IPC authority-readiness consolidation & review

**Phase:** DECOUPLING H8D (offline review / verification — no code change, no activation)
**Base:** `feature/decoupling-hardening` @ `ad4a41a` (H8C PASS)
**Scope:** independently verify the merged H6/H7/H8A/H8B/H8C implementations, consolidate the offline
authority-readiness evidence, and determine whether the program may proceed to **H9A cutover
preparation**. H8D activates nothing. **No production, no real Dhan, no IPC authority, no cutover.**

## Method

Every row below was verified against the **merged** code and re-run tests on `ad4a41a` — not from
prior phase reports. An independent adversarial authority review (the §37 18-question set) reviewed
the consolidated architecture "as if authority were enabled tomorrow" and returned **PASS** (HIGH 0,
authority-readiness-affecting MEDIUM 0). Full suite: **2,677 passed / 9 skipped**; ruff, ruff format,
mypy (`app deploy`, 228) clean. Production `main` `a6b8c68` unchanged; defaults remain **LEGACY_ONLY**.

## Consolidated readiness table

| Requirement | Evidence (merged code + tests) | Status | Remaining gate |
|---|---|---|---|
| **M1** durable producer identity | `epoch.py` file-backed monotonic counter, `flock` serialise, fsync+atomic-replace before return; corrupt → `EpochStateError` (fail-closed); H7 30-incarnation monotonicity | **PASS** | volume-loss non-guarantee documented (ADR-020) |
| **D1** atomic stream+reference | `atomic.py` single Lua call, all validation before first write (ADR-021); atomic/reference suites green | **PASS** | — |
| **M2** bounded async boundary | `boundary.py` bounded queue, single FIFO worker, overflow explicit, worker-fault terminal; no Redis on ingest hot path | **PASS** | — |
| **C1** durable idempotency | `durable_dedup.py` durable Redis authority + hot cache; `consumer.py` apply→mark→ACK, fail-closed on dedup error | **PASS** | — |
| **L1** publication continuity | `continuity.py` accepted vs published distinguished; terminal break sticky per incarnation, cleared only by a new epoch; never sequence arithmetic | **PASS** | — |
| **H6** backend restart | consumer restart needs no Dhan/producer/epoch; durable C1 survives; PEL recoverable (`test_market_ipc_h6_*`) | **PASS** | — |
| **H7** ingestion restart | new epoch strictly increases; `(producer_id, epoch, seq)` distinct; reconnect ≠ epoch bump (`test_market_ipc_h7_*`) | **PASS** | — |
| **B2** apply→mark crash window | H8A reference gate (`tick_engine._is_duplicate_reference`) + value-convergent engine mutations (candle volume = delta of replaced snapshots, no `+=`); C1 dedup before apply; proven against a real `TickEngine` sink | **RESOLVED** | reference-gate ceiling (L1) folds into H9 live-feed/FIX-2 gates |
| **B4** retention vs redelivery horizon | H8B `validate_retention_invariant` (dedup_ttl ≥ horizon + margin) fail-closed at **construction and consumer start**; `BEYOND_HORIZON` consumer age gate (publish-independent) | **RESOLVED** | ADR-028 acceptance |
| **B11** Redis loss detection | H8C stateless `RedisLossDetector.reconcile` — reset/rewind/tail-loss fail-closed, legal gaps preserved, bounded reads; reconciles producer L1 + Redis-native metadata + consumer progress | **RESOLVED** | ADR-029 acceptance; producer-evidence conveyance via `md:health` (H9 wiring) |
| **reference bootstrap** | `BACKEND_REFERENCE_BOOTSTRAP_PROVEN = NO` (unchanged since H5); D1 loader proven only at cutover | **NOT PROVEN** | **H9A** prerequisite |
| **ADR-027** single-Dhan-owner policy | `Proposed`; single-**process** `validate_single_dhan_owner` exists; cross-process interlock is an OPEN decision (I1/I2) | **Proposed / interlock OPEN** | interlock **before H9B**; acceptance before authority |
| **ADR-028** B4 retention invariant | `Proposed`; implementation conforms (H8B) | **Proposed** | acceptance before IPC authority |
| **ADR-029** B11 loss detection | `Proposed`; implementation conforms (H8C) | **Proposed** | acceptance before IPC authority |
| **B10** Dhan secret migration | `DESIGN_RESOLVED / IMPLEMENTATION_PENDING`; backend needs no Dhan creds when legacy off (clean `dhan.env` seam) | **PENDING** | **H9/H10** |
| **single-owner runtime interlock** | not implemented; `NEVER_TWO_LIVE_DHAN_OWNERS` pends the open I1/I2 decision (ADR-027) | **OPEN** | **H9A** design → **H9B** enforce |
| **FIX-2 / live timestamp parity** | `LIVE_DHAN_TIMESTAMP_PARITY = NOT_PROVEN`; FIX-2A PR #63 RC3 INCONCLUSIVE (untouched) | **NOT PROVEN** | FIX-2 track before any live parity/authority claim |

## Configuration / safety posture (verified)

- **Defaults = LEGACY_ONLY:** `market_ingestion_service_enabled`, `ipc_publisher_enabled`,
  `ipc_consumer_enabled`, `ipc_authoritative_enabled` all `False`; `legacy_market_path_enabled=True`;
  `MarketIpcConfig.enabled=False` (`settings.py:150-155`, `config.py:20`).
- **Illegal-combination fail-fast:** `validate_phase_h_flags` rejects `authoritative ∧ legacy`
  (dual authority), `authoritative ∧ ¬consumer`, and the other DESIGN-REVIEW-2 illegal combos, wired
  via `Settings.validate_phase_h_flag_matrix` (verified through the real env-var path).
- **No authority reachable:** nothing composes the consumer/publisher/loss-detector into production;
  the import-boundary arch tests enforce it.

## Adversarial review outcome

18/18 authority questions answered with evidence; **no HIGH, no authority-affecting MEDIUM**. LOW
(all documented, correctly gated to H9): (L1) the reference-gate ceiling — two *distinct*
`previous_close` values on one trading date + an out-of-order reclaim could regress the reference;
this is an ordering property of two distinct events, **not** the B2 "one event twice" guarantee,
and rests on the Dhan feed-constancy assumption (cross-day rejected by the consumer trading-date
gate); (L2) B11 producer-evidence conveyance via `md:health` is H9 wiring; (L3) non-suffix
(middle-entry) loss is outside the threat model; (L4) epoch durable-volume-loss operational
non-guarantee. No overclaims found — every "exactly-once/guaranteed" in `market_ipc` is an honest
disclaimer.

## Readiness verdicts

- **OFFLINE_AUTHORITY_PREREQUISITES_PROVEN = YES** — B2/B4/B11 closed and independently verified;
  M1/D1/M2/C1/L1 + H6/H7 + H4C/H5 parity green.
- **READY_FOR_H9A = YES** — cutover *preparation* may begin (this is where reference bootstrap,
  rollback rehearsal, and the single-owner interlock design/ratification are executed).
- **READY_FOR_H9B = NO** — gates: single-owner interlock implemented (ADR-027 I1/I2); reference
  bootstrap proven; rollback frozen + rehearsed; FIX-2 resolved for parity; ADR-028/029 accepted.
- **READY_FOR_IPC_AUTHORITY = NO** — all H9B gates + ADR-023/025/027/028/029 acceptance +
  `LIVE_DHAN_TIMESTAMP_PARITY` proven + B10 staged + live cutover/parity verification (H9C).

H8D is review-only: no production code changed, no ADR status changed, nothing activated.
