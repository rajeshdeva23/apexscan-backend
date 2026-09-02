# ADR-016 — Sector Intelligence as a Reusable Upstream Market-Context Layer (SECTOR)

| Field | Value |
|-------|-------|
| **Status** | Proposed (reference/domain foundation only; flips no authority bit, enables no strategy) |
| **Date** | 2026-09-02 |
| **Deciders** | Market Intelligence / Platform Architecture |
| **Complements** | ADR-001 (PostgreSQL source of truth), ADR-007 (strategy layering / dependency direction) |
| **Related** | SECTOR-1 (requirements freeze), SECTOR-2 (this slice: domain + membership) |

---

## Context

ApexScan scans individual F&O stocks but has no notion of *sector*. We want a reusable
intelligence layer that answers market-regime / sector-strength / stock-participation
questions and feeds strategies (Open=High/Low, PDH/PDL, Narrow CPR, momentum) as
ranking/context — not as a trade trigger. SECTOR-1 froze the requirements; source
inspection at `eba3a65` established the constraints this ADR commits to:

- The live feed carries **F&O-underlying cash equities only, in quote mode**. NSE
  sector/benchmark **indices are not subscribed**, and **market depth/order-flow is not
  ingested**. So V1 sector strength must be **derived from constituents**, not index prices.
- `MarketContext` is frozen and per-instrument; sector state is cross-instrument and has a
  different cadence, so it does not belong inside `MarketContext`.
- The EventBus fans `MarketContextCreated/Updated` to passive subscribers
  (`CrossInstrumentStrategyScanner`, the R4D evidence observer) — a proven, isolated,
  single-feed integration pattern.
- No sector taxonomy exists in the repo or the provider; `app/models` is empty (no tables).

## Decision

Introduce a **Market Intelligence** bounded context (`app/market_intelligence/`) whose
first layer is **Sector Intelligence**, a read-only consumer of canonical market events
that publishes sector/stock context for strategies and the scanner to rank on.

### D1 — Dependency direction (one-way)
```
market pipeline -> market_intelligence -> strategies / scanner
```
Sector code MUST NOT import `app.strategies`, `app.strategy_manager`, `app.api`, a concrete
provider adapter, or any DB/Redis/transport SDK. It never learns of Open=High/Low, PDH/PDL,
or Narrow CPR — strategies consume sector context, never the reverse. Enforced by an
AST import-boundary test (`tests/architecture/test_sector_import_boundary.py`), mirroring
ADR-007's guards.

### D2 — Primary taxonomy is one-per-stock, distinct from index membership
The engine classifies on exactly one **PRIMARY_SECTOR** per eligible instrument.
`SECTOR_INDEX`, `THEMATIC_INDEX`, and `BROAD_MARKET_INDEX` memberships are overlapping
metadata/context and MUST NOT change the primary classification. These kinds are never
mixed silently (`GroupKind`).

### D3 — Authoritative, governed, effective-dated static dataset (no runtime fetch)
Membership is a **versioned, provenance-bearing static dataset** loaded from disk; the
runtime never scrapes NSE. Primary source = NSE-published classification (Total Market
`Industry`, fallback NIFTY 500); the F&O universe is reproduced offline by the repo's own
`derive_equity_fno_universe`; secondary/broad-market memberships come from NSE index
constituent files. Regeneration is a governed offline step
(`reference_data/generate.py`) — corporate/index changes and F&O add/removes never require
engine-code edits. Memberships are effective-dated (half-open `[from, to)`); resolution is
deterministic for a trading date with no future leakage and no cross-date state.

### D4 — State ownership
Live sector snapshots (SECTOR-3+) live in **process memory** in the intelligence service.
Redis is added only if/when a cross-process API needs it. Membership/config is the static
dataset. **No PostgreSQL table in V1** (ADR-001 remains: PG is the durable source of truth
where durability is actually needed; sector membership is a governed reference dataset).

### D5 — Fail-closed
Unmapped instrument → explicit unmapped status (never guessed, never treated as neutral).
Malformed dataset or violated invariant → raise on load/construction. Later live layers
reduce confidence on stale/missing constituent data rather than fabricate strength.

### D6 — Analysis only
Sector Intelligence has no order placement, no broker/trading API, no position/funds
access, and opens **no provider connection** — market data arrives only via existing
canonical events. It never mutates `MarketContext` and never controls authority.

### D7 — V1 / V2 / V3 boundaries
- **V1:** price-derived constituent strength — breadth, participation, dispersion,
  relative strength vs a universe-median proxy, stock leadership, freshness/confidence.
- **V2:** subscribe NIFTY/sector indices (true index return + benchmark relative strength);
  time-of-day-normalized relative volume (needs historical intraday).
- **V3:** order-flow/depth and OI enrichment (needs a fuller feed).

## Consequences

- A new bounded context and dependency edge (`market_intelligence -> strategies`) are
  committed; strategies gain an optional context input without any coupling back.
- V1 relative strength uses a universe-median proxy, not the true NIFTY index, until index
  instruments are subscribed (V2) — documented, not hidden.
- The membership dataset is a maintained artifact: it must be regenerated (not hand-edited)
  when the F&O universe or NSE classification changes.
- SECTOR-2 lands only the domain vocabulary, the governed dataset, and a deterministic
  resolver. Metrics, scoring, ranking, EventBus subscription, APIs, and strategy
  integration are separate, later slices (SECTOR-3+).

## Rejected alternatives

1. **Sector index price as the strength source** — indices are not subscribed and
   cap-weighting hides heavyweight distortion; constituent-derived strength is both
   available and distortion-resistant.
2. **SectorSnapshot inside `MarketContext`** — `MarketContext` is frozen and per-instrument;
   sector state is cross-instrument with a different cadence.
3. **A PostgreSQL membership table in V1** — no durability need a governed static dataset
   doesn't meet; adds a migration and schema surface for reference data.
4. **Runtime NSE fetch** — couples the engine to a scraped, rate-limited, bot-blocked
   source; a frozen validated snapshot is reproducible and safe.
5. **Overlapping index membership as the primary classification** — not one-per-stock;
   would require an arbitrary tie-break (SECTOR-1 §3 explicitly warned against this).

ADR-001/007 are referenced, not modified.
