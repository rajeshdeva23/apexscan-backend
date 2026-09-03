# ADR-018 — Passive Sector Intelligence Shadow Runtime

| Field | Value |
|-------|-------|
| **Status** | Proposed |
| **Date** | 2026-09-03 |
| **Deciders** | Platform / Market Intelligence Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | ADR-010 (runtime composition/lifecycle), ADR-015 (evidence observer pattern), ADR-016 (sector layer), ADR-017 (canonical previous close), `docs/sector_view/` |

---

## Context

SECTOR-2/3/4 provide pure sector metrics and stock participation; VIEW-1A made `previous_close`
available on the live `MarketContext`. We want to observe these metrics against the live feed
**without** affecting trading, and without a second feed/auth, persistence, API, or calibration.

## Decision

Add a passive, read-only shadow runtime (`app/services/sector_intelligence/`) that:

1. Subscribes to the generic `MarketContextCreated` / `MarketContextUpdated` events on the
   existing synchronous EventBus (a non-throwing O(1) callback).
2. Maintains bounded latest-observation state — one observation per instrument, capped at the
   SECTOR-2 expected universe; unknown identities rejected; deterministic out-of-order,
   duplicate, and trading-date-rollover rules.
3. Runs a single periodic evaluator that captures a coherent state copy and reuses the
   SECTOR-2/3/4 pure engines verbatim to build an internal, immutable `SectorShadowSnapshot`.
4. Is wired into `LiveMarketRuntime` via a factory, mirroring the ADR-015 evidence observer:
   started/stopped with the runtime, gated by `settings.sector_shadow_enabled` (default off).

The runtime produces no public output, no persistence, and no EventBus publication; internal
snapshots are read via in-process accessors returning immutable copies.

## Consequences

- Live sector observability with zero effect on ingestion, engine, strategies, provider health,
  or trading (subordinate error isolation; the callback never propagates into the bus).
- No new feed/auth/network/persistence/API; no change to `session_open` semantics or the
  ADR-009 authority enable path; no change to the Dhan subscription mode.
- The runtime depends only on generic domain, events, and the SECTOR engines (enforced by an
  import-boundary test). It duplicates no metric.
- **Caveat:** canonical `previous_close` support (ADR-017) exists, but live Dhan code-6 delivery
  under the current `TICK` subscription mode is **unverified**; the runtime stays healthy when
  `previous_close` is available for all / some / none of the universe.

## Status note

Proposed. It does not enable itself in production, deploy, or perform live validation. ADR-016
and ADR-017 remain Proposed and are unchanged; no accepted ADR is modified.
