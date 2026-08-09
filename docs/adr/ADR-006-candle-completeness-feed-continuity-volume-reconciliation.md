# ADR-006 — Exact Candle Completeness, Feed Continuity, and Volume Reconciliation

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-07 |
| **Deciders** | Platform / Market Data Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Refines** | ADR-005 (live-volume candle-boundary baseline assumptions) |
| **Related** | `docs/05_DATA_PROVIDER.md` (§6, §11–§12), `docs/06_MARKET_ENGINE.md` (§9, §13, §25), ADR-003, ADR-004, ADR-005 |

---

## Context

ADR-005 made session-cumulative volume the canonical basis for live candle
volume, forbade summing last-traded-quantity (LTQ), and forbade fabricating
missing data. P4.4 implemented candle aggregation on that basis, but a
volume-integrity audit found that ADR-005 conflated two distinct values:

- the **prior finalized bucket's last observed cumulative** (a value at a tick
  time strictly *before* the boundary), and
- the **cumulative volume at the exact candle boundary**.

DhanHQ's v2 live market feed is a **snapshot/update** stream (Ticker/Quote/Full
binary packets carrying aggregate fields such as day-cumulative volume, average
trade price, total buy/sell quantities, and day OHLC). It does **not** guarantee
one packet per executed trade, and it does **not** guarantee an observation
exactly at any candle boundary. Therefore the last-observation-before-boundary
value is generally **not** the boundary cumulative, and using it as the baseline
produces an *approximate* interval volume. Worse, the P4.4 implementation rolls
that baseline forward across **unobserved (gap) buckets** and across
**feed disconnect/reconnect**, silently attributing the volume of an unobserved
period to a later candle — a misattribution ADR-005's own no-fabrication
principle forbids.

## Problem

A canonical finalized `Candle` is consumed downstream as an authoritative,
complete OHLCV fact. If the engine emits candles whose volume is (a) a boundary
approximation, (b) a gap-spanning delta of unknown distribution, or (c) built on
a stale post-reconnect baseline, then downstream strategies compute on wrong
facts that *look* real. The architecture needs an explicit, provider-neutral
rule for when a candle is authoritative versus incomplete, who owns feed
continuity, and how reconciliation restores exactness.

## Decision

### 1. A canonical finalized `Candle` means authoritative, complete OHLCV

A canonical `Candle` is emitted **only** when its open, high, low, close, and
volume are all authoritative for the exact interval. It must never contain
approximate interval volume, volume spanning an unobserved gap, a guessed
baseline, incomplete OHLC, or silently reconstructed values without authoritative
evidence. When OHLCV cannot be proven complete, the engine keeps an **explicitly
incomplete** candle fact and does not emit a canonical `Candle` until
reconciliation succeeds.

### 2. No boundary approximation in a final candle

`LAST_OBSERVATION_BEFORE_BOUNDARY` must not be treated as
`AUTHORITATIVE_BOUNDARY_BASELINE`. Because the feed is snapshot-based and does
not guarantee a boundary observation, the last observed cumulative before a
boundary does not prove the boundary cumulative. It may be used only as
provisional/partial information, never to finalize an authoritative volume.

### 3. Continuity is not boundary exactness

A contiguous sequence of snapshots (no detected disconnect) establishes
**continuity** — that the feed was not knowingly lost — but **not**
**boundary exactness** — that the provider emitted a snapshot at the exact
interval boundary. The two are distinct: continuity is necessary but not
sufficient for an exact boundary baseline. Exactness additionally requires either
a boundary observation or historical reconciliation (§10).

### 4. Gap rule (mandatory correction)

For a non-contiguous bucket progression — e.g. cumulative `10,000` observed
before `09:20`, no observed bucket for `09:20–09:25`, then `15,000` at `09:27` —
the `5,000` delta must **not** be attributed to `09:25–09:30`; its distribution
across the unobserved period is unknown. The engine must:

- detect non-contiguous bucket progression;
- invalidate the carried volume baseline;
- mark the subsequent affected candle volume incomplete;
- **withhold** the authoritative `Candle` until reconciliation.

No zero-volume assumption for an unobserved interval, and no baseline roll across
a missing interval.

### 5. Reconnect rule

A feed disconnect/reconnect that spans one or more bucket boundaries is treated
as a gap (§4): the pre-disconnect baseline is invalidated and affected candles
are marked incomplete until reconciliation. A disconnect wholly within a single
bucket does not misattribute volume (the cumulative delta still bounds that
interval) but yields incomplete OHLC for that interval, which the engine can only
know from a feed-continuity signal (§7). No volume from an unobserved period may
silently be assigned to a later candle.

### 6. Feed-continuity ownership (provider-neutral)

Transport continuity is owned by the **Data Provider** layer, which reports it as
a **broker-neutral continuity fact** consumed by the Market Engine. Conceptually
it expresses events such as `FEED_CONNECTED`, `FEED_DISCONNECTED`,
`FEED_RECONNECTED`, and `CONTINUITY_LOST` (aligning with the honest degraded/loss
signalling of `05 §11`/§12 and `09 §10.4`). It must **not** expose Dhan WebSocket
types, provider response codes, or provider security IDs. The Market Engine
consumes only the neutral fact; it never infers or reconstructs provider
connectivity history itself.

### 7. Provider vs Engine vs P4.5 responsibilities

- **Data Provider:** knows transport continuity; reports continuity loss/recovery
  as the neutral fact; never constructs candles.
- **Market Engine (P4.4):** owns candle completeness; marks live candle data
  incomplete after relevant continuity loss or a detected gap; never fabricates
  missing OHLCV; withholds canonical candles that are not authoritative.
- **Historical Context (P4.5):** owns reconciliation/backfill via the Phase-3
  `HistoricalDataAdapter`; may replace/reconcile incomplete intervals with
  authoritative historical candles and reconstruct aligned cumulative baselines
  when mathematically exact.

These responsibilities are not mixed.

### 8. Candle completeness model

An explicit, broker-neutral completeness/data-quality state is introduced,
using the smallest set of typed concepts that expresses the facts:

| State | Meaning |
|-------|---------|
| `COMPLETE` | Authoritative OHLC and volume; eligible to become a canonical `Candle`. |
| `INCOMPLETE_OHLC` | Price data missing/unobserved for part of the interval. |
| `INCOMPLETE_VOLUME` | OHLC known but no authoritative boundary baseline for volume. |
| `FEED_GAP` | The interval overlaps a detected unobserved/continuity-loss period. |
| `AWAITING_BACKFILL` | Marked for P4.5 reconciliation before it can be authoritative. |

The canonical finalized `Candle` remains a pure authoritative OHLCV fact.
Incomplete live aggregation is represented by a **separate typed
wrapper/state**, never by a `Candle` that pretends to be complete.

### 9. Partial candle semantics

`PartialCandle` (or its successor wrapper) may carry incomplete live
observations — observed OHLC, observed volume information, the completeness/
data-quality state, and bucket identity. It is extended only as needed for those
facts. An authoritative `Candle` is never exposed until the completeness criteria
(§1, §8) are satisfied.

### 10. Session-start semantics

- **Scenario A (engine observes the session from/before the open boundary):** an
  exact session-open volume baseline of **zero must not be assumed automatically**
  unless the canonical/provider contract proves precisely what the cumulative
  counter includes at the *regular-session* boundary (e.g. whether opening-auction
  volume is included). Until that is proven, the first bucket's volume is
  `INCOMPLETE_VOLUME`.
- **Scenario B (engine starts mid-bucket):** OHLC before startup is unknown; the
  candle is `INCOMPLETE_OHLC` (and volume baseline unknown) until authoritative
  backfill/reconciliation. Scenarios A and B are not treated identically.

### 11. Session-candle semantics

`Timeframe.session()` follows the same exactness rule: it becomes a canonical
`Candle` only when complete session OHLC **and** complete authoritative session
volume are known; otherwise it remains incomplete/awaiting reconciliation.

### 12. Historical reconciliation (P4.5) scope and limits

P4.5 may use the Phase-3 `HistoricalDataAdapter` (which returns completed
historical candles with authoritative OHLCV) to:

- replace an incomplete completed interval with authoritative historical OHLCV;
- reconstruct an **aligned** cumulative boundary by summing complete authoritative
  historical interval volumes from session start;
- supply missing first-bucket/session-open facts;
- repair intervals spanning a feed outage.

It must **not** claim arbitrary-boundary reconstruction: an exact cumulative
baseline at a boundary is provable only when the historical resolution aligns to
(or evenly divides) that boundary, the intervals from session start are complete,
and the historical volume shares the live cumulative's semantic.

### 13. Arbitrary-timeframe implication

The generic, duration-based timeframe architecture is unchanged: 1m/5m/7m/15m/…
still require no candle-algorithm branches. Reconciliation for an arbitrary
timeframe (e.g. 7m) is exact only when the historical data resolution can
reconstruct that timeframe's boundaries and contents exactly. This is a
documented limitation of reconciliation, not a weakening of timeframe
extensibility.

### 14. Live-strategy latency trade-off

Consequence recorded: volume-dependent strategies may need to wait for
reconciliation before an authoritative finalized candle is available when the
live feed cannot prove exact boundary volume; price-only live facts remain
available sooner via the incomplete/partial candle. Correctness is never silently
downgraded for latency. If an explicitly-labelled approximate/estimated candle is
ever needed, it requires a separate decision and must **not** reuse canonical
`Candle` semantics.

## Decision Drivers

- A canonical `Candle` is an authoritative fact; downstream must never compute on
  approximate or gap-spanning volume (`06 §25`, §28).
- The provider feed is snapshot-based with no boundary or per-trade guarantee.
- Honesty over completeness: withhold rather than fabricate/misattribute.
- Clean layering: transport continuity is the provider's; completeness is the
  engine's; reconciliation is P4.5's.

## Consequences

### Benefits
- Finalized candles are trustworthy OHLCV facts.
- Gap and reconnect misattribution is eliminated by construction.
- Incompleteness is explicit and analysable, enabling deterministic backfill.

### Trade-offs
- Some finalized candles are delayed until reconciliation (latency vs correctness).
- A new broker-neutral continuity fact and a completeness model must be added.
- The Market Engine and P4.5 must cooperate through the continuity fact and the
  completeness state.

## Alternatives Considered

| Alternative | Why rejected |
|-------------|--------------|
| Emit approximate boundary volume in canonical candles | Downstream treats candles as authoritative; approximation corrupts strategy results silently. |
| Roll the baseline across gaps/reconnects (current P4.4) | Misattributes unobserved-period volume to a later candle; violates no-fabrication. |
| Assume zero session-open baseline unconditionally | Wrong for mid-session startup and unproven at the regular-session boundary. |
| Overload `Candle` with a completeness flag | Pollutes the authoritative fact; incomplete state belongs to a separate wrapper. |

## Relationship to ADR-005

ADR-005 remains **Accepted** historical context and its core decisions stand:
session-cumulative volume is the canonical market fact; LTQ must never be summed;
missing data must never be fabricated. ADR-006 **refines** the specific
live-volume assumptions ADR-005 under-specified: it defines what counts as an
authoritative candle-boundary baseline (not last-observation-before-boundary),
mandates gap and reconnect withholding, separates continuity from boundary
exactness, introduces the completeness model and feed-continuity ownership, and
sets the historical-reconciliation requirements. Where the two overlap on
candle-boundary baseline exactness, ADR-006 governs.

---

*This ADR records a point-in-time decision. If it is ever revised, mark it
`Superseded by` a new ADR rather than editing the decision in place.*
