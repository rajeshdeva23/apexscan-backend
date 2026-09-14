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

## Chosen model (ADR-028): a consumer-side horizon, not a producer trim

The redelivery horizon is enforced **at the consumer**, because any producer-side trim is
publish-triggered and stops exactly when the market is quiet — the low-rate condition B4 exists for.

- `max_redelivery_horizon_seconds` (new) — the maximum time an event may be *applied* after it was
  produced.
- **Consumer age gate:** on every consumed entry (new *and* reclaimed) the consumer computes
  `age = now − produced_at` and, if it exceeds the horizon, terminally ACKs it as `BEYOND_HORIZON`
  **without applying it**. So an aged-out pending entry — reclaimed after a quiet weekend, its dedup
  key possibly expired — is *lost* (safe), never double-applied. This is publish-independent: it
  holds with `MAXLEN`-only trimming and zero publishes.
- **Invariant (fail-closed):** `dedup_ttl_seconds >= max_redelivery_horizon_seconds +
  retention_safety_margin_seconds`, checked at config construction **and** at
  `MarketEventConsumer.start()` (so a `model_copy` that bypasses model validation still cannot start
  an unsafe consumer). Guarantees every reclaim *within* the horizon still finds its dedup key.

Within the horizon → the dedup key exists → C1 suppresses a genuine redelivery. Beyond the horizon →
the consumer refuses to apply → no expired-key double-apply. `MAXLEN ~` stays only as a memory cap.

### Why not a producer time-trim (the rejected first cut)

The first H8B attempt trimmed the stream on publish (`XTRIM MINID`). The adversarial review refuted
it: with no publishes (quiet market) nothing trims, so an aged entry stays live past `dedup_ttl` and
a later reclaim double-applies — inverting the failure mode from safe-loss to unsafe-double-apply,
reachable with shipped defaults (12h horizon < a weekend). Correctness must not depend on publish
activity, so the bound moved to the consumer.

### Why MAXLEN cannot substitute for time

`MAXLEN` bounds a **count**; the horizon is a **time**. Their ratio is the event rate, which varies
(low-volume sessions, market closure, weekends). Correctness cannot depend on rate (§11), so the
bound is a time enforced at the consumer, independent of the count-based memory cap.

## PEL / XAUTOCLAIM interaction

A pending entry whose stream record has been trimmed (by `MAXLEN`) is a tombstone. Redis ≥ 7.0
auto-drops it from the PEL (nil-field entry in a deleted list); Redis 6.2 (the test runtime) yields
`(None, None)` and leaves a dangling PEL entry. The transport reclaim path **skips id-less tombstones
and treats nil-field entries as decode-failures** (terminal-ACK where an id exists), so a trimmed
pending entry never crashes the reclaim loop and is never re-applied (verified over real Redis). A
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

`B2_STATUS = RESOLVED` (ADR-025/H8A) is preserved: H8B touches only `config.py` (invariant),
`consumer.py` (the `BEYOND_HORIZON` age gate + fail-closed start check), and `transport.py` (the
tombstone-robust reclaim) — it does not alter the TickEngine reference gate, the dedup key formula,
epoch-awareness, or the C1 apply→mark→ACK order. Same-sequence / new-epoch identities remain
distinct (verified).

## Storage growth

Bounded by `maxlen` (stream entries) and `dedup_ttl_seconds` (each dedup key is `SET ... EX`).
Neither bound depends on event rate for correctness; rate informs only operational capacity
(peak events/s × horizon ≤ `maxlen`).

## Test evidence

- `tests/unit/test_market_ipc_retention_invariant.py` — the fail-closed invariant: defaults safe,
  unsafe TTL rejected, boundary (equal safe / one below rejected), weekend-length horizon needs a
  long TTL, zero/negative/excessive values rejected, actionable error message.
- `tests/integration/test_market_ipc_h8b_b4_retention_redis.py` (real `redislite`): a pending entry
  reclaimed **after** the horizon with an expired dedup key is dropped, not re-applied (the HIGH-1
  quiet-market scenario, mutation-verified); a reclaim **within** the horizon is suppressed by the
  dedup key; the consumer fails closed at start on a `model_copy`'d unsafe config; a trimmed pending
  entry is neither re-applied nor crashes the loop; the dedup key TTL exceeds the horizon;
  same-seq/new-epoch stay distinct; dedup survives a fresh Redis client.
- `tests/architecture/test_market_ipc_import_boundary.py` — H8B tests touch no
  Dhan/strategy/sector/authority surface.

## Residual risks

- **Redis-version PEL cleanup:** on Redis 6.2 a trimmed-before-applied pending entry leaves a
  harmless dangling PEL tombstone (re-scanned and skipped); Redis ≥ 7.0 auto-cleans it. No
  double-apply on either.
- **Availability vs. correctness:** an event that ages out of the horizon before being applied is
  lost by design; the operator sizes the horizon to the maximum tolerated outage.
- **Clock-skew assumption:** the age gate compares the consumer clock to the producer's
  `produced_at` wall clock (not the Dhan LTT `event_timestamp`, so the +5:30 FIX defect does not
  reach it). A double-apply would require the producer clock to run ahead of the consumer by ≥
  `retention_safety_margin_seconds` (default 1h) — a bounded, NTP-covered assumption that replaces
  the old event-rate dependency.
- **Aware clock at activation (H9):** the age subtraction needs a timezone-aware UTC `now`; when the
  consumer is eventually composed (H9), production wiring must inject one. Nothing composes it today.
- `B11` (durable Redis loss detection) remains `DESIGN_RESOLVED / IMPLEMENTATION_PENDING` (H8C);
  `LIVE_DHAN_TIMESTAMP_PARITY = NOT_PROVEN` (FIX-2A untouched). ADR-028 is **Proposed** — governance
  acceptance is a precondition for authoritative activation (H9).
