# ADR-028 — IPC Durable Dedup Retention Invariant (B4)

| Field | Value |
|-------|-------|
| **Status** | Proposed (implementation on `feature/decoupling-hardening`; not activated — IPC stays off) |
| **Date** | 2026-09-14 |
| **Deciders** | Platform / Market-Ingestion Decoupling Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | ADR-021 (D1 atomic publication), ADR-023 (C1 durable dedup), ADR-025 (Phase-H design; B4); Phase H8B |

> This ADR records the B4 closure implemented in H8B. It adds a fail-closed retention invariant and
> a **consumer-side** redelivery-horizon gate, which ADR-023/025 named but did not specify
> mechanically. It activates nothing: IPC remains off by default and no production composition
> constructs the consumer. Status stays **Proposed** pending governance acceptance, which is a
> precondition for authoritative activation (H9).

## Context (the B4 gap)

C1 (ADR-023) records a durable dedup key per canonical `(producer_id, epoch, sequence)` with a TTL
(`dedup_ttl_seconds`, default 1 day). The stream was trimmed by **`MAXLEN` (a count)** only, and
`XAUTOCLAIM` reclaims a pending entry at any age above `claim_idle_ms` (a *minimum*, not a maximum).
So the time an event stays **redeliverable** = the time it stays in the stream = `MAXLEN /
event_rate` — **unbounded in time** at low volume (a quiet session, a weekend, a holiday).

If the dedup key expires while its event is still redeliverable, a redelivery misses the dedup gate
and applies the event twice. A count bound cannot dominate a time horizon: correctness must hold at
any event rate, including zero.

## Decision

Bound the redelivery horizon **at the consumer**, not by trimming the producer's stream — because
any producer-side trim is publish-triggered and therefore stops exactly when the market is quiet
(the B4 condition). Two parts:

1. **Redelivery horizon is a configured time.** New `MarketIpcConfig.max_redelivery_horizon_seconds`
   is the maximum time an event may be applied after it was produced — the recovery/outage window the
   deployment commits to supporting (intraday + overnight + weekend/holiday + restart slack).

2. **The consumer drops any event older than the horizon (publish-independent).** On every consumed
   entry — new *and* reclaimed — the consumer computes `age = now − produced_at` and, if it exceeds
   `max_redelivery_horizon_seconds`, terminally ACKs it as `BEYOND_HORIZON` **without applying it**.
   So an aged-out pending entry (reclaimed after a quiet weekend, its dedup key possibly expired) is
   **lost — the safe degradation — never double-applied**, regardless of publish activity or stream
   trimming. This holds even with `MAXLEN`-only trimming and zero publishes.

3. **The dedup key must outlive the horizon (fail-closed).**
   `dedup_ttl_seconds >= max_redelivery_horizon_seconds + retention_safety_margin_seconds`, so every
   reclaim *within* the horizon still finds its dedup key and is suppressed by C1. Enforced by a
   `MarketIpcConfig` model validator **and** re-checked at `MarketEventConsumer.start()` (so a config
   mutated via `model_copy` — which bypasses model validation — can still never start the IPC
   consumer/authority path with an unsafe window). The margin covers producer↔consumer clock skew
   and apply-vs-publish delay.

Together: within the horizon the dedup key exists → a genuine redelivery is suppressed; beyond the
horizon the consumer refuses to apply → no expired-key double-apply. The bound is enforced where the
double-apply would occur (the consumer reclaim path), independent of event rate.

Defaults (self-consistent, §14): `dedup_ttl_seconds=86_400` (1d) ≥ `max_redelivery_horizon_seconds`
`=43_200` (12h) + `retention_safety_margin_seconds=3_600` (1h) = 46_800. For weekend-safe recovery,
raise both the horizon and the TTL together (e.g. horizon 4d, TTL ≥ 4d + margin); the validator
rejects an inconsistent pair.

## Why not the rejected alternatives

- **Producer-side time-trim (`XTRIM MINID` on publish).** Publish-triggered ⇒ no trim when the
  market is quiet, which is precisely the low-rate B4 condition; it leaves an aged entry live past
  `dedup_ttl` and inverts the failure mode from safe-loss to unsafe-double-apply. Rejected as the
  correctness mechanism (this was the first-cut H8B design; the adversarial review refuted it).
- **Size `MAXLEN` from an assumed event rate.** Correctness would depend on rate and break at low
  volume / market closure (§11/§12).
- **Never expire dedup keys.** Unbounded Redis key growth (§21).

## Redis semantics and version note

`MAXLEN ~` on `XADD` stays as the stream **memory cap** (bounds count, not time). A pending entry
whose stream record has been trimmed is a **tombstone**: Redis ≥ 7.0 lists its id with nil fields and
auto-drops it from the PEL; Redis 6.2 yields `(None, None)` (no recoverable id) and leaves a harmless
dangling PEL entry. The transport reclaim path **skips id-less tombstones and treats nil-field
entries as decode-failures** (terminal-ACK where an id exists), so a trimmed pending entry can never
crash the reclaim loop or be re-applied. Known 6.2-only residual: a large tombstone backlog can
starve the bounded reclaim page (availability, not correctness); Redis ≥ 7.0 self-heals.

## Storage growth

Bounded by the `MAXLEN` count cap (≤ `maxlen` stream entries) and, for dedup keys, by
`dedup_ttl_seconds` (`SET ... EX`). Neither depends on event rate for correctness; rate informs only
operational capacity (peak events/s × horizon ≤ `maxlen`).

## Consequences

- **B4 → RESOLVED**: the redelivery horizon is enforced at the consumer independent of event rate,
  and the dedup key provably outlives every within-horizon reclaim, fail-closed at construction and
  at consumer start. B2 (ADR-025/H8A) and B11 unaffected.
- Changes: `market_ipc/config.py` (invariant + `validate_retention_invariant`), `market_ipc/
  consumer.py` (`BEYOND_HORIZON` age gate + start-time fail-closed check), `market_ipc/transport.py`
  (tombstone-robust reclaim). No producer change, no authority, no activation; ADR-023/025 semantics
  unchanged.
- **Acceptance of this ADR is a precondition for authoritative activation (H9).**
