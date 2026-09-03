# Sector View — Live Shadow Runtime (SECTOR-VIEW-1) — PARTIAL (provenance blocker)

Goal: a passive live Sector Intelligence shadow runtime over the existing EventBus/Dhan feed,
reusing SECTOR-2/3/4. **Halted at the §C data-provenance gate — not implemented.** The
mandatory input `previous_close` is not available from the live `MarketContext` without either
a production-pipeline change (out of scope this phase) or manufacturing it (forbidden). No
runtime was built; no production/auth/network touched. ADR-016 unchanged (Proposed).

## Provenance review (the deciding step)

| Field | Live source | Status |
|-------|-------------|--------|
| canonical Instrument | `MarketContext.instrument` | **available** |
| trading_date | `MarketContext.session.trading_date` (authoritative classifier) | **available** |
| observation_timestamp | `MarketContext.event_timestamp` / `observed_at` (tz-aware UTC) | **available** |
| last_price | `MarketContext.latest_tick.last_price` | **available** |
| session_open | `MarketContext.latest_tick.session_ohlc.open_price` (raw WS day-OHLC, non-authority, evidence-grade; may be None early) | **available** (shadow-grade) |
| **previous_close** | — none — | **NOT AVAILABLE** |

**Why previous_close is unavailable:**
- Not a field on `Tick`, `ProviderSessionOhlc` (open/high/low/close = current session, not prior
  close), or `MarketContext`.
- `SessionStatistics` (ADR-009) carries authoritative open/high/low only (no previous_close),
  is authority-gated, and is **UNAVAILABLE/OFF** in production.
- The Dhan WS previous-close packet is **decoded and discarded**:
  `app/adapters/dhan/live.py::_decode_previous_close_packet` validates then `return ()`
  ("without inventing a canonical live event") — it never becomes a canonical event or reaches
  `MarketContext`. No `PREVIOUS_CLOSE` handling exists anywhere in the Market Engine.

`SECTOR-3 ConstituentObservation` requires `previous_close` (`Field(gt=0)`), so no live
observation can be constructed. The §C session_open condition itself is satisfiable (raw WS
`session_ohlc.open_price`), but previous_close is the hard blocker.

## Not done (correctly, per §C "do not invent a workaround")

No manufactured previous_close (e.g. from first LTP or session OHLC); no production
adapter/context/pipeline change to surface the discarded packet; no REST/daily fetch; no second
auth/feed. No observer/state/worker runtime implemented, since it could construct no valid
observation.

## Remediation for a future, separately-authorized phase

One of (each a governed change, out of scope here):
1. **Surface the WS previous-close** as a canonical field on `MarketContext`/`Tick` (adapter +
   schema + tick-engine change; likely an ADR-016/ADR-008-adjacent decision). Lowest-latency,
   uses the existing feed.
2. **Extend authoritative session statistics** (ADR-008/009) to include a governed
   previous_close, consumed via the authority path.
3. **Inject an offline/daily previous-close reference** (e.g. the SECTOR-5B adjusted daily
   series once available) into the shadow — a separate governed data path.

Once previous_close has a defensible source, the runtime is straightforward: the
SECTOR-VALIDATION-1 harness (`evaluate_universe`, ~7 ms/210) already implements the exact
orchestration; a passive EventBus observer (per `docs/sector_validation/LIVE_SHADOW_VALIDATION_PLAN.md`)
feeds it. The remaining VIEW-1 docs (LIVE_SHADOW_RUNTIME, SHADOW_SNAPSHOT_CONTRACT,
RUNTIME_SAFETY) are deferred until the provenance blocker is resolved, to avoid specifying a
runtime that cannot yet be built.
