# H8C — B11 closure: Redis loss / continuity reconciliation

**Phase:** DECOUPLING H8C (offline test topology only)
**Base:** `feature/decoupling-hardening` @ `6257bbc` (H8B PASS)
**Scope:** close blocker **B11** — detect Redis durability loss (AOF tail-loss, reset, rewind) by
reconciling producer L1 evidence with Redis stream state and consumer progress, **without** using
producer-sequence gaps as proof of loss. Implements ADR-029. **No live Dhan, no production, no IPC
authority activation, no B2/B4 rework.**

## B11 problem statement

Producer-side L1 confirms delivery at D1's `XADD` ack and **cannot** observe a later Redis loss (AOF
`everysec` tail-loss, `FLUSHALL`/reset, older-snapshot restore). Authority readiness must be able to
tell apart:

- the producer never published it (legal gap / producer failure);
- the producer published it and it is present / pending / lagging / legitimately trimmed (H8B);
- the producer published it but Redis lost or rewound it.

## Evidence sources (reconciled at a point in time; nothing persisted)

| source | fields | note |
|---|---|---|
| producer L1 | `producer_id`, `producer_epoch`, **`last_published_sequence`**, terminal break, outcome uncertain | survives a Redis loss (epoch file is on the ingestion host, not Redis); uses the D1-confirmed *published* position, never accepted/allocated |
| Redis (bounded) | `XINFO STREAM` (length, `last-generated-id`), `XINFO GROUPS` (`last-delivered-id`, `pending`), one `XREVRANGE COUNT 1` → last entry identity | O(1); no unbounded scan; 6.2-compatible (does not need `entries-added`) |
| consumer | last canonical identity durably applied | in producer-sequence space |

## Accepted vs published

L1 separates *accepted* (M2 admitted) from *published* (D1-confirmed). The detector reconciles the
**published** position only — an accepted-but-unpublished event (queue overflow, publish failure)
never counts as something Redis should hold, so it is never mislabelled as loss.

## Why sequence gaps cannot prove loss

M2 allocates a sequence before admission; an overflow/reject leaves a hole, so `100, 102` is a
**legal** gap, not proof `101` was lost. The detector never looks for "the missing 101" — it compares
the producer's last **published** position to the stream's last entry identity. (Verified: T08 legal
gap → HEALTHY.)

## Incarnation scoping

All reasoning is within one `(producer_id, producer_epoch)`. A new epoch resets the producer sequence
to 1 (H7); the detector treats a new epoch as a new incarnation, never a rewind. (Verified: T09.)

## Classifications

`HEALTHY`, `CONSUMER_LAGGING`, `PENDING_RECOVERY`, `RETENTION_EXPECTED` are ready-for-authority; the
producer-cause and Redis-loss states are fail-closed:

| state | signal |
|---|---|
| `HEALTHY` | confirmed publications present; consumer caught up |
| `CONSUMER_LAGGING` | events retained; consumer behind (still ready) |
| `PENDING_RECOVERY` | delivered + pending; awaiting XAUTOCLAIM/ACK (still ready) |
| `RETENTION_EXPECTED` | applied, then legitimately aged out under the H8B horizon (still ready) |
| `PRODUCER_PUBLICATION_FAILED` | L1 reports a terminal break — attributed to the producer, not Redis |
| `REDIS_STREAM_RESET` | stream absent / `last-generated-id == 0-0` / group gone under a live producer |
| `REDIS_STATE_REWIND` | group `last-delivered-id` sorts after the stream's `last-generated-id` |
| `PUBLISHED_EVENT_UNACCOUNTED_FOR` | last entry behind the producer's confirmed published position (tail-loss) |
| `INSUFFICIENT_EVIDENCE` | Redis metadata unavailable, or producer outcome uncertain |

## Producer L1 reconciliation

L1 `BROKEN` (definite failure / overflow / worker fault) → `PRODUCER_PUBLICATION_FAILED` — the cause
is preserved and downstream absence is **not** mislabelled as Redis loss. L1
`PUBLICATION_OUTCOME_UNCERTAIN` (the H3B ambiguous D1 outcome) → `INSUFFICIENT_EVIDENCE`: no claim of
loss or success. (Verified in the pure-reconcile suite via `from_continuity`.)

## H8A / H8B dependencies

H8C reads L1 + Redis-native metadata only; it does not touch the C1 dedup key formula, the H8A
reference gate / apply→mark order, or the H8B retention invariant / consumer age gate. `RETENTION_EXPECTED`
uses the H8B contract (applied-then-aged-out) rather than inventing a second retention rule.

## Fail-closed authority gate

`ready_for_authority` is a **readiness input** to ADR-025's fail-closed authority gate — H8C
activates nothing. Any unresolved reset / rewind / unaccounted-publication / insufficient evidence
keeps IPC authority unavailable.

## Test evidence

- `tests/unit/test_market_ipc_loss_detection.py` — the pure `reconcile` taxonomy (all nine states),
  legal sequence gap → healthy, new epoch → not rewind, tail-loss → unaccounted, uncertain/terminal
  producer handling via `from_continuity`.
- `tests/integration/test_market_ipc_h8c_b11_loss_detection_redis.py` (real `redislite`): healthy /
  lagging / pending over a real stream+group; injected `FLUSHALL` reset; `XGROUP SETID` rewind;
  `XDEL` tail-loss; legal gap; new epoch; restart-with-data-preserved; metadata-unavailable
  fail-closed; **bounded metadata reads** (constant Redis calls over a 2,000-entry stream, no
  `xrange`/`xread`); 10,000-event replay deterministic ×3; duplicate transport not a false alarm.
- `tests/architecture/test_market_ipc_import_boundary.py` — the detector and its test touch no
  Dhan/strategy/sector/authority surface.

## Remaining limitations

- **Producer-evidence conveyance** to the backend (via the frozen `md:health` snapshot) is an
  activation concern wired at H9, not H8C; the detector takes the producer snapshot as input here.
- **Redis version:** `entries-added`/`entries-read`/`lag` (Redis ≥ 7.0) would add redundancy; the
  6.2 test runtime lacks them, so correctness rests on `last-generated-id` / `last-delivered-id`.
- ADR-029 is **Proposed** — governance acceptance is a precondition for authoritative activation
  (H8D readiness review / H9). `LIVE_DHAN_TIMESTAMP_PARITY = NOT_PROVEN` (FIX-2A untouched).
