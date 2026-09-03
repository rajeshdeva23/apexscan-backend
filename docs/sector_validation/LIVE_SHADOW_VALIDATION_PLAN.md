# Live-Shadow Validation Plan (SECTOR-VALIDATION-1)

How ApexScan would feed **real** live observations into the validated harness, read-only, in a
future phase. **Design only — not wired here.** Wiring production requires explicit
authorization and (per ADR-016 §D1 direction) a subordinate/new ADR before implementation.

## Architecture recommendation (future SECTOR-6-shadow)

- **Passive EventBus subscriber**, exactly like `CrossInstrumentStrategyScanner` and the R4D
  evidence observer: subscribe to `MarketContextCreated/Updated`; keep **one bounded latest
  observation per constituent** (identity → last_price/session_open/previous_close/
  observation_timestamp from the frozen `MarketContext`). **No** second WebSocket, **no**
  second Dhan auth, **no** new provider, **no** index subscription — it reuses the single
  existing production feed.
- **Callback = O(1)**: update the latest snapshot for that instrument (or set a dirty flag).
  It must **never** compute all sectors synchronously inside a tick callback, must wrap work in
  `try/except` and never re-raise (the bus has no subscriber isolation — ADR-015 §D2), and must
  do no REST/disk/JSON/blocking in the callback.
- **Snapshot cadence**: a separate bounded task evaluates the universe via `evaluate_universe()`
  on a controlled cadence (recommended **every 60 s** plus the named checkpoints below), not
  per tick. Benchmark shows a full evaluation is **~7 ms** for 210 constituents, so a 60 s
  cadence is ~0.01% duty cycle — negligible; even 1 s would be safe if ever needed.
- **Output**: write the `to_artifact()` evidence JSON to a bind-mounted artifacts path (like
  R4D), never to Git. Read-only; it changes no MarketContext, authority, strategy, or trading
  state.

## Observation checkpoints (validation only, not production thresholds)

09:20, 09:25, 09:30, 09:45, 10:00, 10:30, 11:00, 12:00, 13:00, 14:00, 15:00 IST — to observe
how metrics evolve intraday. These are **not** calibrated cadences or thresholds.

## Freshness caveat

Historical candle replay cannot fully calibrate live tick freshness (SECTOR-5A). A live shadow
run is where `freshness_limit` behavior is genuinely observed (stale-instrument handling under
real feed gaps/reconnects). The validated harness already fails closed on stale/missing.

## Safety invariants for any live shadow run

Read-only; single existing feed; no auth/token generation; passive subscriber; bounded memory
(O(universe)); non-blocking callback; no MarketContext mutation; no authority/strategy/trading;
market-state-aware (evaluate only during LIVE_SESSION, per the authoritative classifier);
fail-closed on membership/date/duplicate/stale.

## Explicitly out of scope until separately authorized

Production deployment, the live subscriber code, a runtime `SectorSnapshot` API, Redis/DB
persistence of live snapshots, strategy consumption, and **all** frontend/dashboard/heatmap
work (the user owns visual design — a frontend phase must pause for the user first).
