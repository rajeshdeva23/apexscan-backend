# ADR-028 — IPC Durable Dedup Retention Invariant (B4)

| Field | Value |
|-------|-------|
| **Status** | Proposed (implementation on `feature/decoupling-hardening`; not activated — IPC stays off) |
| **Date** | 2026-09-14 |
| **Deciders** | Platform / Market-Ingestion Decoupling Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | ADR-021 (D1 atomic publication), ADR-023 (C1 durable dedup), ADR-025 (Phase-H design; B4); Phase H8B |

> This ADR records the B4 closure implemented in H8B. It changes the durable **retention
> contract** of the IPC transport (adds a time-based stream trim + a fail-closed config invariant),
> which ADR-023/025 named but did not specify mechanically. It activates nothing: IPC remains off by
> default and no production composition constructs the transport. Status stays **Proposed** pending
> governance acceptance, which is a precondition for authoritative activation (H9).

## Context (the B4 gap)

C1 (ADR-023) records a durable dedup key per canonical `(producer_id, epoch, sequence)` with a TTL
(`dedup_ttl_seconds`, default 1 day). The transport trimmed the stream by **`MAXLEN` (a count)**
only. `XAUTOCLAIM` reclaims a pending entry at any age above `claim_idle_ms` (a *minimum*, not a
maximum). So the time an event stays **redeliverable** = the time it stays in the stream = `MAXLEN /
event_rate` — **unbounded in time** at low volume (a quiet session, a weekend, a holiday).

If the dedup key expires while its event is still redeliverable, a redelivery misses the dedup gate
and the event is applied twice. A count bound cannot dominate a time horizon: correctness must hold
at any event rate, including zero.

## Decision

Close B4 with an explicit, fail-closed **time** invariant plus a **time-based stream trim** that
makes the invariant sound.

1. **Redelivery horizon is a configured time.** New `MarketIpcConfig.max_redelivery_horizon_seconds`
   is the maximum time an event may remain redeliverable — the recovery/outage window the
   deployment commits to supporting (must cover the longest backend/consumer outage: intraday +
   overnight + weekend/holiday + restart slack).

2. **The producer age-trims the stream to that horizon.** D1 (`RedisAtomicPublisher`, the sole
   production producer) trims on every publish with `XTRIM ... MINID <server_now_ms − horizon>`,
   computed from the Redis server clock (`redis.call('TIME')` inside the atomic Lua — the same clock
   that stamps stream IDs, never a client clock). The trim is **exact** (no `~`), so the horizon is a
   hard bound: no entry survives past it, hence none is redeliverable past it. `MAXLEN ~` stays on
   the `XADD` as an independent **memory cap**; whichever bound trims first only *shortens*
   redeliverability, which is always safe for dedup.

3. **The dedup key must outlive the horizon (fail-closed).**
   `dedup_ttl_seconds >= max_redelivery_horizon_seconds + retention_safety_margin_seconds`, enforced
   by a `MarketIpcConfig` model validator that raises at construction. An unsafe configuration
   cannot construct, so it can never start the IPC consumer/authority path (§13/§29). The margin
   covers apply-vs-publish delay and clock skew (with exact trimming there is no approximate-trim
   overhang to absorb).

Defaults (self-consistent, §14): `dedup_ttl_seconds=86_400` (1d) ≥ `max_redelivery_horizon_seconds`
`=43_200` (12h) + `retention_safety_margin_seconds=3_600` (1h) = 46_800. For a weekend-safe
deployment, raise both the horizon and the TTL together (e.g. horizon 4d, TTL ≥ 4d + margin); the
validator rejects an inconsistent pair.

## Why not the rejected alternatives

- **Size `MAXLEN` from an assumed event rate.** Forbidden: correctness would depend on rate and
  break at low volume / market closure (§11/§12).
- **Never expire dedup keys.** Unbounded Redis key growth (§21) with no lifecycle.
- **Approximate age-trim (`MINID ~`).** Leaves an overhang (entries in a macro node with any young
  entry survive), so the horizon is not a hard bound and the margin must cover an unbounded macro
  node. Exact `MINID` gives a true bound.

## Redis semantics and version note

Supported behaviour targets Redis ≥ 6.2 (`XAUTOCLAIM`, `XTRIM MINID`). A pending entry whose stream
record has been trimmed is a **tombstone**: Redis ≥ 7.0 lists its id with nil fields and auto-drops
it from the PEL; Redis 6.2 yields `(None, None)` (no recoverable id) and leaves a harmless dangling
PEL entry. The transport now **skips id-less tombstones and treats nil-field entries as
decode-failures** (terminal-ACK where an id exists), so a trimmed pending entry can never crash the
reclaim loop or be re-applied. A trimmed-before-applied event is *lost* (an availability tradeoff the
operator sizes via the horizon), never double-applied — B4 concerns double-apply only.

## Storage growth

Bounded by the `MAXLEN` count cap (≤ `maxlen` stream entries) and, for dedup keys, by
`dedup_ttl_seconds` (each key `SET ... EX`). Neither depends on event rate for correctness; rate is
used only for operational capacity estimates (peak events/s × horizon ≤ `maxlen`).

## Consequences

- **B4 → RESOLVED**: the redelivery horizon is a hard time bound and the dedup key provably outlives
  it, enforced fail-closed. B2 (ADR-025/H8A), B11 unaffected.
- The change is in `market_ipc/config.py` (invariant) and `market_ipc/atomic.py` (D1 age-trim) plus
  a transport reclaim-robustness fix; no authority, no activation, no ADR-023/025 semantics changed.
- **Acceptance of this ADR is a precondition for authoritative activation (H9).**
