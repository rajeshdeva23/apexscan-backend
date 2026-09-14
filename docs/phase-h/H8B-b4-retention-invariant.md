# H8B — B4 closure: durable dedup vs. redelivery-horizon retention invariant

**Phase:** DECOUPLING H8B (offline test topology only)
**Base:** `feature/decoupling-hardening` @ `57ba40b` (H8A PASS)
**Scope:** close blocker **B4** — the durable C1 dedup key must remain available at least as long as
its event can legitimately be redelivered, or an expired dedup identity lets a still-redeliverable
event miss the dedup gate and re-apply. Implements ADR-028. **No live Dhan, no production, no IPC
authority activation, no B2/B11 work.**

## The B4 risk

C1 (ADR-023) writes a durable dedup key per canonical `(producer_id, epoch, sequence)` with a TTL
(`dedup_ttl_seconds`, default 1 day). Before H8B the stream was trimmed by **`MAXLEN` (a count)**
only, and `XAUTOCLAIM` reclaims a pending entry at any age above `claim_idle_ms` (a *minimum*). So an
event stayed redeliverable for `MAXLEN / event_rate` — unbounded in time at low volume. If the dedup
key expired first, a redelivery re-applied the event:

```
apply E → record dedup(E) → ACK lost / E still in stream
        → dedup(E) TTL expires → E reclaimed → contains(E)=false → apply E again  (B4)
```

## Old (pre-H8B) contract

`dedup_ttl_seconds=86_400` (from apply), `maxlen=100_000` (approximate, count-based),
`claim_idle_ms=30_000` (reclaim *minimum*). No validation tied dedup TTL to any redelivery horizon;
the horizon was unbounded in time. Audit confirmed count-based `MAXLEN ~` trimming in both the
transport `XADD` and the D1 atomic Lua — no time bound anywhere.

## Chosen model (ADR-028)

A **time** invariant made sound by a **time-based trim**:

- `max_redelivery_horizon_seconds` (new) — the maximum time an event may remain redeliverable.
- The D1 producer age-trims the stream every publish with an **exact** `XTRIM ... MINID
  (server_now − horizon)` (server clock via `redis.call('TIME')`), so no entry survives past the
  horizon. `MAXLEN ~` stays as an independent memory cap.
- Invariant (fail-closed at config construction):
  `dedup_ttl_seconds >= max_redelivery_horizon_seconds + retention_safety_margin_seconds`.

### Why MAXLEN cannot substitute for time

`MAXLEN` bounds a **count**; the horizon is a **time**. Their ratio is the event rate, which varies
(low-volume sessions, market closure, weekends). Correctness cannot depend on rate (§11), so the
bound must be a time — hence exact `MINID` trimming, not count trimming.

## PEL / XAUTOCLAIM interaction

A pending entry whose stream record has been trimmed is a tombstone. Redis ≥ 7.0 auto-drops it from
the PEL (nil-field entry in a deleted list); Redis 6.2 (the test runtime) yields `(None, None)` and
leaves a dangling PEL entry. The transport reclaim path now **skips id-less tombstones and treats
nil-field entries as decode-failures** (terminal-ACK where an id exists), so a trimmed pending entry
never crashes the reclaim loop and is never re-applied (verified over real Redis). A
trimmed-before-applied event is *lost* (availability, sized by the horizon), never double-applied.

## Weekend / holiday behaviour

The invariant scales: to survive a Fri→Tue outage, set `max_redelivery_horizon_seconds` to the
long-weekend duration and `dedup_ttl_seconds` to at least that plus the margin. The validator rejects
a horizon the dedup TTL cannot cover, so a deployment cannot silently assume continuous trading.

## Dedup TTL semantics

`record` is `SET key 1 EX dedup_ttl_seconds` — retention runs from the most recent successful apply.
`contains` is `EXISTS` and never refreshes the TTL, so a duplicate *lookup* cannot slide the
lifetime; only a crash-window re-`record` refreshes it, which can only *extend* (safe). The in-memory
cache is non-authoritative — a fresh process reads the durable key (verified: dedup survives a new
Redis client).

## H8A interaction

`B2_STATUS = RESOLVED` (ADR-025/H8A) is preserved: H8B touches only `config.py` (invariant) and
`atomic.py` (age-trim) plus the transport reclaim fix — it does not alter the TickEngine reference
gate, the dedup key formula, epoch-awareness, or the C1 apply→mark→ACK order. Same-sequence /
new-epoch identities remain distinct (verified).

## Storage growth

Bounded by `maxlen` (stream entries) and `dedup_ttl_seconds` (each dedup key is `SET ... EX`).
Neither bound depends on event rate for correctness; rate informs only operational capacity
(peak events/s × horizon ≤ `maxlen`).

## Test evidence

- `tests/unit/test_market_ipc_retention_invariant.py` — the fail-closed invariant: defaults safe,
  unsafe TTL rejected, boundary (equal safe / one below rejected), weekend-length horizon needs a
  long TTL, zero/negative/excessive values rejected, actionable error message.
- `tests/integration/test_market_ipc_h8b_b4_retention_redis.py` (real `redislite`): the producer
  age-trims the stream to the horizon (old entries evicted, first entry gone); the dedup key TTL
  exceeds the horizon; a trimmed pending entry is neither re-applied nor crashes the loop;
  same-seq/new-epoch stay distinct; dedup survives a fresh Redis client.
- `tests/architecture/test_market_ipc_import_boundary.py` — H8B tests touch no
  Dhan/strategy/sector/authority surface.

## Residual risks

- **Redis-version PEL cleanup:** on Redis 6.2 a trimmed-before-applied pending entry leaves a
  harmless dangling PEL tombstone (re-scanned and skipped); Redis ≥ 7.0 auto-cleans it. No
  double-apply on either.
- **Availability vs. correctness:** an event that ages out of the horizon before being applied is
  lost by design; the operator sizes the horizon to the maximum tolerated outage.
- `B11` (durable Redis loss detection) remains `DESIGN_RESOLVED / IMPLEMENTATION_PENDING` (H8C);
  `LIVE_DHAN_TIMESTAMP_PARITY = NOT_PROVEN` (FIX-2A untouched). ADR-028 is **Proposed** — governance
  acceptance is a precondition for authoritative activation (H9).
