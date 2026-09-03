# Sector Shadow Runtime — Safety Guarantees (SECTOR-VIEW-1B)

The shadow runtime is subordinate: it must never affect ingestion, the Market Engine, the
EventBus, provider health, strategies, the scanner, or trading. This documents how that holds.

## Error isolation

- The EventBus is synchronous and **propagates subscriber exceptions** into the publisher
  (ingestion). The observer callback (`_on_context`) is therefore a strict non-throwing
  boundary: any exception is caught, counted (`events_rejected`), logged, and swallowed.
- The periodic evaluator catches all non-cancellation exceptions inside `evaluate_once`; a
  failure increments `snapshot_failures`, logs, and **preserves the last-good snapshot** rather
  than replacing it with a partial/exception state. The driver loop (`run`) also guards each
  tick, so one failure never ends the driver.
- The evaluator task has a done-callback in `LiveMarketRuntime`: a non-cancel end is logged,
  never made fatal to the runtime ("shadow stopped, ingestion intact").

## Boundedness

- State holds **one latest observation per instrument**, keyed by canonical identity. Unknown
  identities are rejected and never stored, so cardinality never exceeds the SECTOR-2 expected
  universe (dynamically resolved — the `210` figure is never hardcoded).
- Duplicate events replace in place (no second entry). Diagnostics are fixed integer/float
  counters — no unbounded history or per-tick logging.

## Ordering and session integrity

- **Out-of-order:** an observation older than the stored one is rejected
  (`out_of_order_events`); the newer observation stays authoritative — state never rewinds.
- **Duplicate:** an equal-timestamp observation is an idempotent replace (`duplicate_events`).
- **Rollover:** driven solely by `MarketContext.session.trading_date` — never UTC midnight or
  local wall-clock. A forward trading-date change clears all prior-session state (`rollovers`)
  so Day-D observations contribute zero to Day-(D+1); a backward date is a late prior-session
  event and is rejected (`late_trading_date_events`).

## Non-overlapping evaluation

A single evaluator task runs the pure math synchronously (no `await` inside the computation), so
it cannot overlap itself. A re-entrancy guard additionally makes a concurrent entry (evaluation
slower than the cadence) a no-op that increments `evaluation_overruns` and returns the current
snapshot — no second calculation starts, and state is copied coherently before any work.

## Lifecycle

- `sector_shadow_enabled=false` (default) ⇒ **0** observer subscriptions and **0** evaluator
  tasks — zero behavioral difference.
- `sector_shadow_enabled=true` ⇒ exactly **1** observer (subscribed to `MarketContextCreated`
  and `MarketContextUpdated`) and exactly **1** evaluator task. `start()` is idempotent (no
  duplicate subscription or task). `shutdown()` cancels and awaits the task and unsubscribes the
  observer. The provider lifecycle is unaffected.

## What it never does

No DB read/write, no Redis, no REST/historical/provider calls, no network, no disk persistence,
no public REST/WS API, no frontend, no strategy execution, no change to `session_open` semantics
or the SessionStatisticsAuthority / ADR-009 enable path, and no change to the Dhan subscription
mode. It never fabricates a missing `previous_close`, `session_open`, or `last_price`.

## Architecture boundary

`app/services/sector_intelligence/` imports only generic domain (`app.market_engine`,
`app.schemas`), events (`app.events`), the SECTOR engines (`app.market_intelligence`), and
itself. It imports no Dhan adapter, auth, socket/HTTP/transport SDK, DB, Redis, strategy, or
order/execution module — enforced by `tests/architecture/test_sector_shadow_import_boundary.py`.
