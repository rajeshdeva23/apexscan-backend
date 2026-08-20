# ADR-011 Addendum — Multi-Interval Special Trading Sessions

| Field | Value |
|-------|-------|
| **Type** | Subordinate addendum (not a numbered ADR) |
| **Subordinate to** | ADR-011 — Historical Trading-Calendar Authority Window (and its calendar-exception-model addendum) |
| **Status** | Accepted |
| **Date** | 2026-08-14 |
| **Deciders** | Market-Engine Architecture |
| **Corrects** | The single-contiguous-interval assumption of `TradingSessionOverride` in the ADR-011 calendar-exception-model addendum (M9) and its implementation (ADR-011-MODEL-IMPL). No prior Accepted decision is rewritten. |

---

## Why this addendum

The ADR-011 calendar-exception-model addendum (M9) modelled an exceptional OPEN date's
hours as a **single** continuous `live_start → live_end` (`TradingSessionOverride`).
ADR-011-MODEL flagged multi-interval sessions as an unverified risk (its §7 STOP). That
risk is now **proven real** by primary NSE Capital-Market evidence, so the canonical model
must be corrected before any dataset is provisioned (DATA-R1).

## Authoritative evidence assessed (supplied externally; not re-fetched)

| Circular | Date | Subject | CM scope | Live-market structure (IST) |
|----------|------|---------|----------|------------------------------|
| **NSE/MSD/60677** | 2024-02-14 | Special Live trading session Sat **2024-03-02**, intra-day DR switch-over | Explicit | **Two disjoint blocks:** 09:15–10:00 **and** 11:30–12:30 |
| **NSE/MSD/61893** | 2024-05-07 | Special Live trading session Sat **2024-05-18**, intra-day DR switch-over | Explicit | **Two disjoint blocks:** 09:15–10:00 **and** 11:30–12:30 |
| **NSE/CMTR/72349** | 2026-01-16 | Live Trading Session **2026-02-01** (Union Budget) | Explicit | **One block:** 09:15–15:30 |
| **NSE/CMTR/71775** | 2025-12-12 | 2026 CM trading-holiday list; Muhurat on Sun **2026-11-08** | Explicit | Muhurat timing "to be notified subsequently" (⇒ intraday NOT_PROVEN until notified) |
| **NSE/CMTR/72260** | 2026-01-12 | Adds Thu **2026-01-15** as a CM holiday (Maharashtra elections) | Explicit | closed date |

> These citations are the **governance evidentiary basis** only. Actual dates/times enter
> the repository solely as DATA-R1 provisioning, never as runtime behaviour (§ provider
> neutrality). They appear here to document what invalidated the single-interval assumption.

## The exact contradiction

For 2024-03-02 / 2024-05-18 the market was **closed** 10:00–11:30. The single-interval
model can only encode:
- `09:15 → 12:30` — **fabricates** 90 minutes of tradable time across the closed gap; or
- one of the two blocks — **omits** valid trading.

Both are wrong, so the ADR-011 H3 implementation is insufficient for authoritative intraday
history across such days.

## Code assumptions found (single-interval)

`app/market_engine/session.py`: `SessionBounds(regular_open, regular_close)`,
`TradingSessionOverride(trading_date, live_start, live_end)` (single `.bounds`),
`EffectiveSchedule.bounds_for(date) -> SessionBounds` (one interval per date).
Downstream one-interval consumers: `buckets.py` (`bucket_bounds`, `session_buckets`),
`historical/resampling.py` (per-date single bounds), `historical/service.py`
(planner capacity `_candles_per_session`/`_session_seconds` and boundary localization),
`historical/session_candles.py` (session identity from single bounds — unchanged for the
whole-session timeframe).

---

## Decisions

- **MI1 — Multiple live intervals are required.** An exceptional OPEN date must be able to
  declare one *or more* disjoint live-market intervals.
- **MI2 — Canonical interval type.** Introduce a broker-neutral, immutable
  `TradingInterval(start: time, end: time)` with `start < end`. It is the single canonical
  per-interval unit; the bucket algorithm operates on one `TradingInterval` at a time. To
  avoid a dual/ambiguous representation, `SessionBounds` and `TradingInterval` are unified
  into ONE type in R2-IMPL (implementation may rename `SessionBounds`→`TradingInterval` or
  make `SessionSchedule.bounds` yield a `TradingInterval`); there must be exactly one
  interval type across buckets/schedule/override.
- **MI3 — Session-override representation.** `TradingSessionOverride(trading_date,
  live_intervals: tuple[TradingInterval, ...])` with `len ≥ 1`. The single-interval
  `live_start`/`live_end` fields are **removed** (replace, don't deprecate); a one-interval
  override is a one-element tuple (a convenience constructor for the common case is allowed).
- **MI4 — Ordering.** `live_intervals` are stored/validated in chronological order by
  `start` (reject out-of-order input; do not silently sort authoritative data — construction
  fails fast so the dataset is corrected at source).
- **MI5 — Overlap rule.** Overlapping intervals are **rejected** (fail fast).
- **MI6 — Touching-interval rule.** Consecutive intervals must have a **strictly positive
  gap** (`intervals[i].end < intervals[i+1].start`); touching intervals are **rejected**
  (Option C). Rationale: a genuinely continuous session carries no closure and must be one
  interval; allowing touching intervals would inject an artificial bucket boundary at the
  join and make representation non-canonical/ambiguous. This keeps bucketing deterministic.
- **MI7 — Capacity.** `capacity(date, Δ) = Σ capacity(interval_i, Δ)` over live intervals;
  the closed gap contributes **zero**. Worked example (verify against the real bucket
  algorithm in R2-IMPL, do not hardcode): 09:15–10:00 (45m) + 11:30–12:30 (60m) at Δ=5m →
  9 + 12 = **21**, never 195m/5 = 39.
- **MI8 — Bucket generation.** Generate buckets **independently within each interval**
  (anchored at that interval's `start`, final bucket truncated at that interval's `end`),
  then concatenate chronologically. Bucket indices form one contiguous 0-based sequence
  across intervals (the gap consumes no index). A single-interval date is byte-identical to
  today.
- **MI9 — Gap semantics.** The market-closed gap between intervals yields **no buckets and
  no capacity**; a candle spanning `interval_i.end → interval_{i+1}.start` must never be
  created.
- **MI10 — Session/day OHLC.** Ordinary aggregation across all intervals: OPEN = first
  trade/candle of the first interval; HIGH = max over all intervals; LOW = min over all
  intervals; CLOSE = last trade/candle of the final interval; volume = existing governed
  aggregation summed across intervals. No special-session-specific candle rule is invented.
- **MI11 — Completeness.** A multi-interval day is complete iff **every required bucket of
  every declared interval** is present. The inter-interval gap is never "missing".
- **MI12 — Planner.** Per-date contribution = summed interval capacities (MI7). Intraday
  lookback resolution iterates previous trading days summing each date's (possibly
  multi-interval) capacity until the lookback is covered. Boundary localization for a
  multi-interval boundary date uses the **first interval's start** and the **last interval's
  end**; the intervening closed gap simply yields no source candles. No fixed/continuous
  first-open→final-close capacity assumption.
- **MI13 — H3 refinement.** "Authoritative session timing" now means the **complete, ordered
  live-interval set** for that date. `special OPEN + complete intervals → intraday may
  proceed`; `special OPEN + missing/incomplete intervals → intraday FAIL CLOSED`. No
  default-schedule substitution.
- **MI14 — Missing/incomplete interval metadata.** There is **no partial representation**:
  an OPEN date either fully declares all its live intervals (override present) or has none →
  intraday fails closed (`MissingSessionTimingError`). Date/session-level authority may still
  hold without intervals (M17 of the exception-model addendum).
- **MI15 — Migration.** `live_start`/`live_end` are removed in R2-IMPL; all callers move to
  `live_intervals`. The only current callers are ADR-011-MODEL-IMPL tests and the optional
  `overrides` passthrough in `strategy_requirements_wiring.py` (production wires none), so the
  migration is contained. No dual representation may coexist.
- **MI16 — Effective schedule lookup.** `EffectiveSchedule.intervals_for(date) ->
  tuple[TradingInterval, ...]` replaces `bounds_for`; an ordinary date returns a
  single-element tuple `(default interval,)`. `has_override` retained. No leakage to adjacent
  dates.
- **MI17 — Ordinary-session behavior.** Byte-identical: ordinary and single-interval dates
  resolve to a one-element interval tuple; only genuinely multi-interval dates exercise new
  paths. All existing ordinary/historical tests must remain green unchanged.
- **MI18 — Current-day isolation.** `supports_current_day=False`; current-day withheld;
  guarantee NOT PROVEN. Unchanged; this addendum does not enable current-day.
- **MI19 — Provider neutrality & determinism.** No Dhan/provider types, no NSE circular IDs,
  no security IDs in the model; pure, date-driven, deterministic (no `today()`/`now()`,
  no network). Circular IDs live only in this governance doc and (later) DATA-R1 provenance.
- **MI20 — DATA-R1 contract.** After R2-IMPL, DATA-R1 may provision: multi-interval overrides
  (the 2024 Saturday sessions as two intervals each), single-interval overrides (2026-02-01),
  closed dates (2026-01-15 and the 2026 holiday list), and a date-level OPEN with intraday
  fail-closed (Muhurat 2026-11-08 until its timing circular is supplied). Each datum stays
  primary-source-cited.

---

## Live candle engine scope (MI-adjacent)
The multi-interval model is a **historical** concern. The live `CandleEngine` continues to
use the default `SessionSchedule` (one continuous session) via the shared bucket algorithm;
R2-IMPL must keep the live path byte-identical (it passes the default bounds/one interval).
Current-day exceptional-session *live* scheduling is a separate future concern, explicitly
out of scope here (no scope creep).

## Future implementation contract (R2-IMPL — do NOT implement now)
Introduce `TradingInterval`; unify with `SessionBounds`; change `TradingSessionOverride` to
`live_intervals` (validate ≥1, ordered, non-overlapping, strictly-gapped); replace
`EffectiveSchedule.bounds_for`→`intervals_for`; make `buckets.py` generate per-interval and
concatenate; thread multi-interval bounds through `resampling.py` and the planner capacity;
refine H3 to require the complete interval set; migrate the MODEL-IMPL tests/passthrough.

## Future test matrix
- Interval type: `start<end`; equal/inverted rejected.
- Override: ≥1 interval; out-of-order rejected; overlapping rejected; touching rejected;
  duplicate date rejected; one-interval convenience equals prior behavior.
- Capacity: two-block day sums per-interval capacity (e.g. 9+12=21), never spans the gap.
- Buckets: per-interval anchoring; no cross-gap bucket; contiguous global index; single
  interval byte-identical.
- Completeness: all-intervals-present = complete; a missing interval bucket ≠ complete; gap
  never counted missing.
- H3: complete intervals + intraday → proceeds with correct capacity/boundaries; missing
  intervals + intraday → `MissingSessionTimingError`; date/session-level still resolves.
- Session OHLC: open/high/low/close/volume aggregate across intervals via ordinary rules.
- Ordinary: unchanged; live path unchanged.
- Isolation: `supports_current_day=False`; authority bits False; no Dhan types; guards green.

## Invariants preserved
`supports_current_day=False`; `staged_observation_verified=False`;
`tick_aggregate_verified=False`; production `compose_market_runtime(calendar_coverage=None)`
→ `UnavailableHistoricalWarmup` (fail-closed); no concrete strategy; broker-neutral Market
Engine.

## Consequences
**Positive.** The canonical model can represent real NSE multi-block Capital-Market sessions
truthfully; intraday capacity/completeness are correct; the closed gap is never fabricated as
tradable. **Negative/accepted.** DATA-R1 remains blocked until R2-IMPL lands. **Neutral.** No
production code changes in this governance phase.
