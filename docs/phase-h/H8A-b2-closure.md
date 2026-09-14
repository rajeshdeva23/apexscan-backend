# H8A — B2 closure: authoritative-sink apply→mark crash window

**Phase:** DECOUPLING H8A (offline test topology only)
**Base:** `feature/decoupling-hardening` @ `743d509` (H7 PASS)
**Scope:** close blocker **B2** — the window where a canonical event is applied to authoritative
state, the process (or its durable C1 mark) is lost, and the SAME event is redelivered and
re-applied. Implements the frozen ADR-025 resolution; adds the one residual gate it named. **No
live Dhan, no production, no IPC authority activation, no B4/B11 work.**

## The B2 failure

C1's consumer order is `contains → apply → mark → ACK`. If the process dies (or the durable mark's
Redis write fails) *after* a successful sink apply but *before* the mark commits, the entry stays
pending, is reclaimed (`XAUTOCLAIM`), `contains` returns false, and the sink applies the same
logical event a second time. H4/H5/H6/H7 deliberately surfaced this at the compare-only shadow sink
as `known_b2_duplicate`. H8A makes that second application **harmless for the authoritative sink**.

## Frozen design used (EXISTING_B2_DESIGN)

ADR-025 "Authoritative sink contract" + blocker table freeze B2 as **value-convergence, plus a
reference gate**:

- **B1 (DESIGN_RESOLVED):** every authoritative market-value mutation is `replace` / `max` / `min` /
  delta-of-a-replaced-snapshot with **no local accumulators**, so re-applying an identical event
  corrupts no market value; C1 dedups by canonical identity *before* apply.
- **B2 (DESIGN_RESOLVED_IMPLEMENTATION_PENDING):** value-convergent; benign ordinal/version churn;
  **H8 hardening = "reference gate / atomic apply+mark"** because `MarketReference` was the one
  authoritative mutation with no dedup gate.

No new architecture (no DB journal, no Redis transaction, no idempotency ledger, no
event-sourcing). ADR policy §39: **no new ADR** — this implements the frozen ADR-025 decision.

## Authoritative mutation inventory

The authoritative sink is the `TickEngine` (+ `CandleEngine`). Every state write on the live-apply
path, verified in code:

| mutation | op | duplicate-safe? |
|---|---|---|
| `state.latest_tick` / `latest_quote` (`tick_engine.py:275-276`) | replace | yes — and gated by the Tick/Quote value-equality DUPLICATE gate before it is even reached |
| `state.last_event_timestamp` (`:277`) | replace | yes — also the STALE watermark that rejects an out-of-order reclaim |
| `state.session_statistics` / staged obs (`:280-281`) | replace (whole snapshot) | yes |
| candle `high`/`low`/`close` (`candle_engine.py:356-358`) | max / min / replace | yes |
| candle interval volume (`:365,421`) | `last_cumulative - baseline`, both **replaced snapshots** | yes — never `+=` |
| candle rollover `incomplete.append` (`:371`) | append on bucket change only | yes — a same-tick re-apply hits the already-open bucket (`:297`); bounded deque |
| `MarketReference.previous_close` (`tick_engine.py:317-328`) | replace | value-convergent, **was ungated** → the reference gate (below) |
| `sequence` / context `version` (`sequence.py:36`, `context.py:393`) | `+= 1` monotonic ordinal | benign churn — a new version number, never a market value |

**Unsafe accumulators found: none** on the authoritative apply path. The only `+=` are the monotonic
sequence/version ordinals (benign churn) and O(1) diagnostic counters (not market state). This was
confirmed by an exhaustive independent sweep of `market_engine/`.

## The reference gate (the one code change)

`TickEngine._accept_reference` now rejects a reference whose `previous_close` equals the
instrument's current value **within the same session** as `DUPLICATE` — no version, no publish, no
mutation — the reference-path analogue of the Tick/Quote value-equality gate. The gate is
**session-scoped** (`_is_duplicate_reference`): a genuine new-session reference (a different
classified trading date) always applies, **even when its value coincides with the prior session's
close — a flat close** — so it re-stamps the new session and `previous_close` survives the rollover
carry-forward (`_carried_previous_close`) instead of being cleared to `None`. A reference carries no
`event_timestamp`, so it can have no STALE watermark; a genuine new value still applies. C1's
`contains → apply → mark → ACK` order is unchanged (no mark-before-apply, so no opposite "mark then
crash before apply" loss).

**Known ceiling (honest scope).** If ≥2 *distinct* `previous_close` values reached one instrument
within a reclaim horizon **on the same trading date**, a delayed reclaim of the older reference
(no timestamp watermark) could regress `previous_close`. This is not reachable under the real feed:
Dhan's previous-close is the prior session's close, constant intraday (`adapters/dhan/live.py`
surfaces it verbatim, no intraday correction), and a cross-day reference is rejected by the
consumer's trading-date gate (`consumer.py`) before it reaches the engine. Giving the engine a
producer-sequence watermark would couple it to transport identity and is out of the frozen design.
Ticks/quotes have no such ceiling — the STALE timestamp watermark rejects any out-of-order reclaim.
Tests model one reference per instrument (reality) and the flat-close rollover explicitly.

## Crash matrix

| point | behaviour | proof |
|---|---|---|
| C0 before dup check | nothing applied; redeliver applies once | baseline replay |
| C1 after dup check / before apply | nothing mutated; redeliver applies once | baseline replay |
| C2 during apply | engine assigns the new context atomically at the end; a fault leaves prior state | engine construction (all-or-nothing per event) |
| **C3 after apply / before durable mark** | reclaim re-delivers; engine DUPLICATE / reference gate → no second mutation | T04/T05 (tick), T16 (reference) |
| **C4 after mark / before ACK** | C1 `contains` suppresses the reclaim; engine never re-invoked | T06 |
| C5 after ACK | normal; no redelivery | baseline |

## Delivery vs. state guarantee

Transport stays **at-least-once** (Redis redelivery is real and is *not* removed). What H8A
establishes is that **authoritative state mutation is idempotent / crash-redelivery safe** under the
canonical `(producer_id, epoch, sequence)` identity. The pipeline is **not** relabelled
exactly-once.

## Shadow sink diagnostic role

The compare-only `RecordingShadowSink` stays deliberately non-idempotent: it records every raw
delivery, so `shadow_compare` still surfaces `known_b2_duplicate` — the diagnostic view that a
duplicate physically arrived. The authoritative engine sink is separately idempotent. The H8A
integration tests keep a raw-delivery counter to prove the duplicate genuinely reached the sink
(non-tautological) while the authoritative version did not advance.

## Tests

- `tests/unit/market_engine/test_tick_engine_b2_idempotency.py` — engine-level duplicate safety:
  Tick/Quote/reference DUPLICATE gates (T03/T11/T12/T16/T17), candle OHLC + volume convergence
  (T13/T14), no market-value accumulator (T15), instrument isolation (T18), no mark-before-apply
  loss (T22), and a **10,000-event** property replay where every identity is redelivered at its
  recovery position and the final context (values AND version) is identical to the clean baseline
  (T23/§35).
- `tests/integration/test_market_ipc_h8a_b2_authoritative_redis.py` — the same closure end-to-end
  over real `redislite` with the C1 consumer driving a real `TickEngine`: healthy baseline, the
  apply→mark crash window for a tick (T04/T05) and a reference (T16), the ACK-lost window (T06), and
  a 2,000-event duplicate-stress replay (~1/7 forced through the crash window) whose authoritative
  state matches the clean baseline exactly (T33/T34).
- `tests/architecture/test_market_ipc_import_boundary.py` — H8A drives the engine but touches no
  Dhan/strategy/sector/services/FIX surface (`market_engine` is intentionally permitted as the sink
  under proof).

## Status after H8A

- **B2 → RESOLVED** for the authoritative sink: value-convergent mutations + the reference gate;
  crash-redelivery safe within a producer incarnation and across consumer/backend restart.
- **B4** (retention / dedup TTL horizon) — `DESIGN_RESOLVED / IMPLEMENTATION_PENDING` (H8B).
- **B11** (durable Redis loss detection) — `DESIGN_RESOLVED / IMPLEMENTATION_PENDING` (H8C).
- `LIVE_DHAN_TIMESTAMP_PARITY = NOT_PROVEN` (FIX-2A remains INCONCLUSIVE, untouched).
- H8A does not enable IPC, activate the consumer/publisher, make the TickEngine authoritative, or
  begin cutover. H8B/H8C/H8D do not start automatically.
