# ADR-007 — Open=High / Open=Low Strategy Specification

Status: Accepted (offline implementation; DEPLOY-10 R3). Not enabled in production
(`STRATEGIES_ENABLED` unchanged; runtime enablement is gated by ADR-009 CSOA22).

## Context

Open=High and Open=Low are the first **current-session** scanner strategies. Unlike
the completed-session plug-ins (Narrow CPR, Previous-Session *), they read the
authoritative current-session open/high/low (ADR-008/009) and re-evaluate on every
tick, so a candidate can qualify and later invalidate within the same session.

## Decision

### Identity
- `open_high` — "Open = High"; `open_low` — "Open = Low". Version 1.0.0.
- Category `OPENING_SESSION`; emission `EDGE_TRIGGERED`; trigger `ON_TICK`.

### Inputs (authority)
- Both declare `fact_needs = (SESSION, SESSION_STATISTICS)` and a
  `FactFreshnessRequirement(SESSION_STATISTICS, max_age = 2 minutes)`.
- The Strategy Manager readiness gate is the **single authority-enforcement point**:
  a strategy is admitted only when `MarketContext.session_statistics.quality ==
  AUTHORITATIVE`, its `trading_date` matches the session, and it is within `max_age`.
- `evaluate()` also fails closed (defence in depth): absent/unauthoritative statistics
  → `SKIPPED` (`SESSION_STATISTICS_UNAVAILABLE`). No tick price, candle extremum,
  previous-session value, or process-start extremum is ever substituted.

### Qualifying condition (exact, no tolerance)
Over authoritative `SessionStatistics` (which guarantees `low <= open <= high`):
- Open=High: `open_price == high_price` → `MATCHED` (`OPEN_EQUALS_HIGH`); else
  `NO_MATCH` (`HIGH_ABOVE_OPEN`).
- Open=Low: `open_price == low_price` → `MATCHED` (`OPEN_EQUALS_LOW`); else
  `NO_MATCH` (`LOW_BELOW_OPEN`).
Equality is exact `Decimal` numeric equality (provider prices are canonical Decimals;
trailing-zero scale differences compare equal). No percentage/tick tolerance is used.

### Direction (scanner classification only; never a trade signal)
- Open=High → bearish opening structure; Open=Low → bullish. Encoded via reason code
  and category. The current scanner API does not project a `direction` field
  (see "API").

### Invalidation lifecycle
Because the strategy re-evaluates each accepted `MarketContext` version, a later tick
that pushes the high above the open (Open=High) or the low below the open (Open=Low)
yields `NO_MATCH`. The cross-instrument scanner keeps one snapshot per strategy,
higher `context_version` wins, and only `MATCHED` records with the ranking metric are
eligible — so an invalidated instrument is naturally removed from ranking (ADR-012).
No separate invalidation engine is introduced.

### Ranking metric
`session_range_pct = (high_price - low_price) / open_price * 100` (Decimal), ordered
DESCENDING (widest open-to-extreme travel first — the strongest opening structure).
For an exact match the "distance from open" is zero, so range is used instead of a
false-precision distance metric. `score` is left `None` (no governed absolute scale).

### Session lifecycle
Session identity is the exchange-local `trading_date` from `MarketSessionClassifier`.
A new trading date produces a fresh scanner snapshot; yesterday's qualification never
persists. Weekend/holiday/special sessions are handled by the trading calendar; no
calendar dates are hard-coded in the strategy.

### Performance
Evaluation is O(1) per context (a Decimal comparison + small metric build); no I/O,
DB, Redis, provider call, or per-symbol task. Scales to the 210-instrument universe.

## Consequences
- Membership in the production catalog does not enable the strategies; only
  `STRATEGIES_ENABLED` does, and runtime readiness additionally requires an
  authoritative current-session source (ADR-009 CSOA22 — one verified source bit).
- A future scanner-API enhancement may project `direction`/`reason_codes`/session
  fields; deferred (see ADR-012 scanner REST API addendum).
