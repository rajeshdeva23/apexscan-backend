# Live Sector Shadow Runtime — Architecture (SECTOR-VIEW-1B)

Package: `app/services/sector_intelligence/`.

## Flow

```
Existing Dhan live feed
  → existing Market Engine (TickEngine)
  → MarketContextCreated / MarketContextUpdated
  → existing in-process EventBus (synchronous)
  → SectorShadowObserver._on_context     (O(1), non-throwing)
  → ObservationState                      (bounded: one latest observation per instrument)
  → SectorShadowRuntime.evaluate_once     (single periodic evaluator, off the callback)
  → SECTOR-2 MembershipResolver + SECTOR-3 + SECTOR-4 (pure engines, reused verbatim)
  → SectorShadowSnapshot                  (internal, immutable, last-good retained)
```

The observer and evaluator are wired into `LiveMarketRuntime` exactly like the ADR-015 evidence
observer: a factory builds the runtime over the shared bus, `start()` subscribes it and launches
one evaluator task, and `shutdown()` cancels the task and detaches the observer. Both are gated
by `settings.sector_shadow_enabled` (default `false`).

## Reuse (no duplicated math)

| Stage | Reused symbol |
|-------|---------------|
| Membership / expected universe | `MembershipResolver` (SECTOR-2) |
| Constituent input | `ConstituentObservation` (SECTOR-3) |
| Universe benchmark | `calculate_universe_proxy` (SECTOR-3) |
| Sector metrics | `calculate_sector_metrics` (SECTOR-3) |
| Stock ranking | `rank_sector_constituents` (SECTOR-4) |

The evaluator re-implements no metric; it only orchestrates the pure functions over a coherent
copy of live state, mirroring the SECTOR-VALIDATION-1 harness pattern (it does **not** import
that evidence tool — the runtime depends only on generic domain, events, and the engines).

## Input provenance (source-verified)

All fields are read from the generic `MarketContext`; no provider field is reconstructed.

| Field | Source path |
|-------|-------------|
| instrument identity | `instrument_identity(MarketContext.instrument)` → `"NSE:SYMBOL"` |
| last_price | `MarketContext.latest_tick.last_price` |
| previous_close | `MarketContext.previous_close` (VIEW-1A canonical field) |
| session_open | `MarketContext.latest_tick.session_ohlc.open_price` (raw WS day-OHLC) |
| trading_date | `MarketContext.session.trading_date` (authoritative classifier) |
| observation_timestamp | `MarketContext.event_timestamp` |
| evaluation_timestamp | injected clock (`Clock.now()`) at evaluation start |

`session_open` uses only the existing raw/evidence-grade WS session-OHLC open already carried on
the context — never a first-observed LTP, first callback price, or candle approximation. The
SessionStatisticsAuthority and the ADR-009 enable path are untouched.

## Concurrency model

The EventBus is **synchronous, single-threaded, in-process** (`app/events/bus.py`): a
`publish()` invokes each subscriber inline on the publishing (ingestion) task. There are no
threads. The observer callback runs inline and does only O(1) work.

The periodic evaluator is a single `asyncio` task on the same event loop. It captures a coherent
copy of the state **synchronously** (a `tuple(...)` with no `await`), so no callback can
interleave mid-copy on the cooperative single-threaded loop — the evaluation always sees a
consistent snapshot. The subsequent pure math runs on that immutable copy. No locks are
required; a re-entrancy guard prevents overlapping evaluations (see RUNTIME_SAFETY.md).

## Completeness and freshness

A `ConstituentObservation` is built only for instruments whose `last_price`, `previous_close`,
`session_open`, and `trading_date` are all present for the current session. Instruments missing
any field are excluded and counted (never defaulted to zero). Freshness uses the single SECTOR-3
rule (`0 <= evaluation_time − observation_time <= freshness_limit`); stale and future-dated
observations are excluded and lower coverage. There is no second freshness definition.

## Configuration

- `sector_shadow_enabled` (bool, default `false`)
- `sector_shadow_interval_seconds` (float, default `60`, bounded `(0, 3600]`) — operational
  cadence only, **not** a trading/calibration/signal threshold.
