# ADR-008 — Tick-Aggregate Current-Session OHLC Evidence Procedure (DEPLOY-10 R2)

Status: Proposed (evidence procedure only). Flips no authority bit. Complements
ADR-008, ADR-009, and ADR-009 CSOA (enable path). This document defines the smallest
rigorous procedure to close CSOA9 (intraday high/low correctness) and CSOA16
(reconnect / session-to-date continuity) for the **WebSocket tick-carried** source
(`Tick.session_ohlc` → `SessionStatistics`, `tick_aggregate_verified`). It does not
edit any accepted ADR; on acceptance an evidence record is appended as a new dated
verification-record artifact.

## A1 — The claim under test
"For a Dhan quote received during the authoritative current trading session,
`ProviderSessionOhlc` (`Tick.session_ohlc`) represents the instrument's
**session-to-date** regular-session Open/High/Low — invariant to ApexScan's
subscription/startup time and to WebSocket reconnect/resubscribe — and rolls to a
fresh session on a new authoritative `trading_date`." Covers open, high, low, late
startup, reconnect, resubscription, multiple instruments, and session rollover.

## A2 — Provider contract (documented vs empirical vs inferred)
- DOCUMENTED (Dhan v2 feed): the quote/full packet carries day open/high/low/close.
  Field carriage is proven (ADR-008 provider-verification-record).
- NOT DOCUMENTED / must be shown empirically: that these are session-to-date extrema
  (not since-subscription), and that they are re-sent complete after reconnect (CSOA16).
- INFERRED (not acceptable as proof): that "day OHLC" == authoritative session OHLC.
External requirement: no Dhan document guarantees post-reconnect session-to-date
re-send; this must be shown by observation.

## A3 — CSOA9 oracle
Primary oracle: **Dhan `/marketfeed/ohlc`** (REST session statistics), compared against
`Tick.session_ohlc`. Caveat: NOT fully independent (both originate from Dhan) — it
proves WS/REST internal consistency, not ground truth. Secondary corroboration:
completed intraday 1-minute candles' running max/min over the session (tick-derived,
independent transform) as a sanity bound. Comparison spec: full 210-instrument
universe; ≥3 cadence points spread across a live session (early/mid/late); Decimal
exact compare after canonical scale-normalisation; align by `as_of`/observation
instant within a small window; tolerance = 0 for open, ≤1 tick for high/low drift
between non-simultaneous samples; classify each as MATCH / DRIFT / MISMATCH.

## A4 — High/low monotonicity
Within one `trading_date`: open stable; high non-decreasing; low non-increasing across
successive samples, and values do NOT reset after a late start / reconnect /
resubscribe. Failure = any open change, high regression, or low regression not
explained by a trading-date rollover.

## A5 — CSOA16 reconnect continuity
On a naturally occurring (or later controlled, non-production) reconnect: capture
pre-reconnect `(O1,H1,L1)` and the first valid post-reconnect `(O2,H2,L2)`. Require
`O2 == O1` (session open), `H2 >= H1`, `L2 <= L1` (unless a bound was invalid), and
reconciliation with the A3 oracle. Verify the reconnect does not create a new session
(`trading_date` unchanged) and `InstrumentState.session_statistics` is not reset by
transport reconnect (already true in code — reconnect never touches the registry).

## A6 — Late-start
Subscribe/restart after meaningful extrema have formed (e.g. high at 09:30, subscribe
11:00). The first valid post-subscription `session_ohlc` must still contain the 09:30
high (and the session open/low), reconciling with the oracle — proving midday bootstrap.

## A7 — Session rollover
At the next authoritative `trading_date` (calendar/classifier-driven, not `date.today()`),
statistics reset to a fresh snapshot; no previous-day O/H/L leaks. Weekend/holiday/
special sessions follow the trading calendar.

## A8 — Evidence artifact (immutable, per CSOA20)
Fields: provider; source (`tick_aggregate`); date/session (`trading_date`); instrument
sample (all 210); observation timestamps; oracle (`/marketfeed/ohlc` + intraday-candle
corroboration); per-instrument comparison results; mismatch list; reconnect evidence;
late-start evidence; rollover evidence; tool version; source SHA; verdict.

## Acceptance criteria
- ACCEPTED: 0 MISMATCH on open; high/low within the stated bound across all samples;
  CSOA16 reconnect continuity holds; late-start and rollover hold; ≥ the agreed sample
  size and universe coverage. → authorises flipping `tick_aggregate_verified = True`
  (a one-line composition change; CSOA20), unblocking Open=High/Open=Low.
- REJECTED: any open mismatch, unexplained high/low regression, or post-reconnect
  session reset. → bit stays False.
- INCONCLUSIVE: insufficient samples / no reconnect observed / oracle unavailable.
  → collect more; bit stays False.

## Scope note
This procedure targets the WS tick source only. `staged_observation_verified`
(REST-staged) and `supports_current_day` (historical current-day reconciliation)
retain their own separate evidence and are out of scope here.
