# ADR-005 — Canonical Session Cumulative Volume for Live Candle Aggregation

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-06 |
| **Deciders** | Platform / Market Data Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Refined by** | ADR-006 (candle-boundary baseline exactness, gap/reconnect, completeness) |
| **Related** | `docs/05_DATA_PROVIDER.md`, `docs/06_MARKET_ENGINE.md` (§9, §13), `docs/02_DATABASE_DESIGN.md` (§6), ADR-003, ADR-004, ADR-006 |

---

## Context

Phase 4.4 introduces live candle aggregation in the Market Engine. A canonical
`Candle` (`app/schemas/market_data.py`) carries OHLC plus a **required**
`traded_quantity` (the interval's traded volume). To build a live candle the
engine must therefore derive a correct, non-fabricated interval volume from the
canonical live event stream it consumes.

The frozen Phase-3 canonical live contracts expose no volume suitable for this:

- `Tick` carries `last_price` and an optional `traded_quantity`. In the Dhan
  adapter (P3.4) that `traded_quantity` is populated from the provider's
  **last-traded-quantity (LTQ)** field — the size of the *most recent trade* at
  that snapshot, not an interval or session aggregate.
- `Quote` carries best bid/ask and their sizes (order-book state).
- `DepthSnapshot` carries the order book. None carries traded volume.

The Dhan standard feed **does** provide a day-cumulative traded-volume figure: it
is decoded in both the Quote packet (response code 4) and the Full packet
(response code 8), validated as non-negative, and then **discarded** — it never
reaches a canonical contract. So today no canonical live event carries the
information required to compute a correct candle volume.

## Problem

**Why LTQ cannot produce candle volume.** `Tick.traded_quantity` (LTQ) is a
per-snapshot value, not an incremental or cumulative one. The V1 live feed is a
quote/snapshot feed, not a trade-by-trade feed: consecutive snapshots may repeat
the same last trade, and trades occurring between snapshots are never observed.
Consequently:

- Summing LTQ across the ticks in a bucket **double-counts** repeated snapshots
  and **undercounts** unobserved trades — it does not equal the interval's true
  traded volume.
- LTQ is optional and may be absent.

Producing a candle from summed LTQ would fabricate a plausible-but-wrong volume,
which `06 §25`/§28 (“mark unavailable, never fabricate; a wrong value is worse
than a missing one”) forbid. A candle cannot be emitted at all without a volume,
because `Candle.traded_quantity` is required.

**Why session cumulative volume is required.** The exchange feed reports a
monotonically non-decreasing **session-cumulative traded volume**. Interval
(candle) volume is the increase of that cumulative figure across the interval.
This is the only correct, provider-supplied basis for candle volume; the engine
must therefore consume a canonical, broker-neutral session-cumulative volume.

## Decision

Introduce a broker-neutral optional canonical fact,
**`session_cumulative_volume: int | None`**, representing the exchange-reported
**session-to-date cumulative traded quantity** for an instrument.

### Canonical owner — the `Tick`

`session_cumulative_volume` is added to the canonical **`Tick`** contract. The
owner is selected on semantic grounds, not because the Candle Engine currently
reads `Tick`:

- Cumulative session volume is a **trade-activity aggregate** — the running sum
  of traded quantity. The `Tick` is the canonical **trade event** (last-traded
  price and last-trade quantity). A running total of traded quantity is a
  trade-side fact that belongs with the trade event, not with the `Quote`'s
  order-book (bid/ask) state or the `DepthSnapshot`.
- The V1 quote-mode feed emits **only** a `Tick` (no canonical `Quote` is built
  from the Quote packet). Placing cumulative volume on `Quote` would leave the
  primary live mode with no volume at all. The trade aggregate must ride the
  `Tick` to exist in V1.

The field is **optional** (`int | None`, default absent) so the contract change
is additive and backward-compatible: existing `Tick` producers and consumers are
unaffected, and events without volume information remain valid.

### Provider neutrality

The canonical field is broker-neutral. Provider-specific field names, packet
layouts, and codes (e.g. Dhan's day-`volume` field in the Quote/Full packets)
remain **adapter-private** and are mapped to `session_cumulative_volume` inside
the Dhan adapter's normalization step, per ADR-003. No provider identifier or
raw payload crosses the Data Provider boundary.

### Candle-volume derivation — boundary delta, not first-observed delta

Candle volume is computed as a **boundary delta** against an authoritative
baseline, never as `last_cumulative_in_bucket − first_cumulative_in_bucket`
(which undercounts trades that occurred before the first observed snapshot in
the bucket):

```text
candle_volume
    = last_cumulative_volume_observed_in_bucket
    − authoritative cumulative-volume baseline at the bucket's opening boundary
```

For a continuous stream, the **prior finalized bucket's ending cumulative
volume** is the next bucket's opening baseline. The first bucket of a session is
baselined at the session's opening cumulative volume.

### Session-reset and validity semantics

- Cumulative volume is **non-decreasing within a trading session**.
- A **reset** to a lower value at a **new trading session** is valid (a fresh
  session starts its own cumulative count).
- A **decrease within a session** (not at a session boundary) is invalid/stale
  data and must be rejected by the engine's validation (`06 §9`); it must not
  reduce state or produce a candle.
- The resulting `candle_volume` must always be **≥ 0**. A computation that would
  yield a negative volume indicates missing baseline or invalid input and must
  not produce a candle.

### Missing-baseline / mid-session startup — complete-or-withhold

If the engine begins receiving data after a bucket has already started and lacks
an **authoritative** cumulative-volume baseline for that bucket, it must **not**
fabricate volume. It follows complete-or-withhold semantics (`06 §6.5`, §25):

- obtain an exact baseline only through an architecture-approved backfill source
  (owned by the Data Provider / P4.5 historical context), **or**
- mark/withhold that candle until exact OHLCV construction is possible.

P4.4 must not introduce historical fetching itself to solve this; it consumes an
exact baseline if one is available and otherwise withholds the affected candle.

### No persistence

`session_cumulative_volume` is ephemeral live market data. Consistent with
`02 §6` (market context and ticks are ephemeral; PostgreSQL is the source of
truth for durable outcomes only, ADR-001), this ADR introduces **no persistence,
no new table, and no repository**. The value lives only in the transient live
stream and the engine's in-memory working state, and is reconstructable from the
feed.

## Decision Drivers

- Candle volume must be correct or absent — never fabricated (`06 §25`, §28).
- LTQ is structurally incapable of yielding interval volume (see Problem).
- Session-cumulative volume is the exchange-authoritative basis for interval
  volume via boundary deltas.
- The canonical owner must be chosen by semantics (trade aggregate → `Tick`) and
  must be reachable in V1's quote-mode feed.
- The change must be additive and broker-neutral, preserving ADR-003.

## Consequences

### Benefits

- Live candles can carry a correct, exchange-derived `traded_quantity`.
- The change is additive and backward-compatible (optional field).
- Provider volume semantics stay contained in the adapter (ADR-003 upheld).
- Complete-or-withhold prevents silent, plausible-but-wrong volumes.

### Trade-offs

- The frozen Phase-3 canonical live contract gains one optional field, requiring
  adapters to map the provider's cumulative volume and requiring the Market
  Engine to track a per-bucket baseline.
- Mid-session startup without an exact baseline yields withheld candles until an
  approved baseline source exists — a deliberate honesty-over-completeness cost.

## Alternatives Considered

| Alternative | Decision | Why |
|-------------|----------|-----|
| Sum `Tick.traded_quantity` (LTQ) per bucket | Rejected | Double-counts snapshots and undercounts unobserved trades; fabricates volume. |
| Put `session_cumulative_volume` on `Quote` | Rejected | Cumulative volume is a trade aggregate, not order-book state; and V1 quote-mode emits no `Quote`, so volume would be unavailable. |
| New standalone volume event contract | Rejected | Unnecessary surface; cumulative volume co-occurs with the trade (`Tick`) and belongs there. |
| `last_in_bucket − first_in_bucket` delta | Rejected | Undercounts trades before the first observed snapshot in the bucket. |
| Make `Candle.traded_quantity` optional and emit OHLC-only | Not chosen | Weakens the canonical `Candle`; boundary-delta from cumulative volume yields complete OHLCV without degrading the contract. |
| Fabricate a baseline on mid-session startup | Rejected | Violates no-fabrication (`06 §25`, §28.28). |

## Implementation Implications (for P4.4, after this ADR)

- Add optional `session_cumulative_volume: int | None` (`ge=0`) to `Tick`.
- Map the Dhan Quote/Full packet cumulative-volume field to it in the Dhan
  normalizer (the value is already decoded today and currently discarded);
  provider specifics stay adapter-private.
- The Candle Engine derives interval volume by boundary delta against the prior
  finalized bucket's ending cumulative baseline, enforces non-decreasing/within
  ≥ 0 semantics, and withholds candles lacking an authoritative baseline.
- No persistence, no new tables, no repositories.

## Relationship to Existing ADRs and Governing Documents

- **ADR-003 (Broker Adapter Pattern).** Unchanged and upheld: normalization from
  the provider's cumulative-volume field into `session_cumulative_volume` occurs
  inside the adapter; no provider type or field name crosses the boundary.
- **ADR-004 (NSE Cash-Equity V1 Domain).** Unchanged: this ADR adds a volume fact
  to the same canonical NSE cash-equity live stream; it selects no derivative
  domain.
- **ADR-001 (PostgreSQL Source of Truth).** Unchanged: no durable state is added;
  cumulative volume is ephemeral live data.
- `docs/05_DATA_PROVIDER.md` continues to own normalization mechanics;
  `docs/06_MARKET_ENGINE.md` §9/§13 continues to own validation and candle
  aggregation. This ADR records the canonical contract change they depend on.

---

*This ADR records a point-in-time decision. If it is ever revised, mark it
`Superseded by` a new ADR rather than editing the decision in place.*
