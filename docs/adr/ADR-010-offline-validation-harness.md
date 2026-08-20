# ADR-010 Addendum — Offline Validation Harness (dev-only)

| Field | Value |
|-------|-------|
| **Type** | Subordinate governance addendum (not a numbered ADR) |
| **Subordinate to** | ADR-010 — Live-Market Runtime Composition & Managed Ingestion Lifecycle |
| **Related** | ADR-012 scanner REST API addendum, ADR-012 Narrow CPR frontend integration, ADR-007 partial-universe historical readiness, ADR-013 production strategy registration |
| **Status** | Accepted |
| **Date** | 2026-08-19 |
| **Deciders** | Platform / Runtime Architecture |
| **Decision** | Add a **dev/validation-only** offline application composition (`app.offline`) that serves the real scanner REST surface over the real runtime pipeline using offline doubles — in-memory DB/Redis lifecycles and a synthetic 208-instrument fixture provider — so the frontend can be accepted against a real backend snapshot without PostgreSQL, Redis, or Dhan. The production composition root (`app.main`) is unchanged. |

---

## Context

`app.main` gates startup on PostgreSQL then Redis, and only wires the market runtime when
`market_provider_enabled=true` — which requires live Dhan credentials. Local UI acceptance of the
Narrow CPR dashboard therefore had no supported way to produce a genuine, non-null scanner snapshot
without external infrastructure and a broker account (see the BLOCKED result of
NARROW-CPR-V1-LOCAL-UI-ACCEPTANCE-R1). The existing 208-instrument offline E2E test already proves
the pipeline works with test doubles injected through two existing seams:
`create_app(lifecycle=…)` and `LiveMarketRuntimeDependency(adapter=…)`.

## Decision detail (OH1–OH8)

- **OH1 — Dev-only, isolated.** The harness lives under `app/services/offline_harness/` with a
  dedicated entrypoint `app/offline.py`. It is never imported by `app/main.py`; the production
  composition and its DB/Redis/Dhan gating are byte-for-byte unchanged.
- **OH2 — Reuse existing seams, no new production surface.** It injects an
  `OfflineFixtureProvider` through the pre-existing `LiveMarketRuntimeDependency(adapter=…)` test
  seam and offline DB/Redis lifecycles through `create_app(lifecycle=…)`. No production code path
  is modified.
- **OH3 — In-memory DB/Redis lifecycles.** `InMemoryDatabaseLifecycle` / `InMemoryRedisLifecycle`
  satisfy the `DatabaseDependency` / `RedisDependency` protocols as verified no-ops, so startup
  completes without external servers. They are unreachable from production.
- **OH4 — Synthetic, honest fixture data.** The provider serves a deterministic 208-instrument NSE
  cash-equity universe whose candles satisfy `H+L+C=300` (pivot 100), so `cpr_width_pct = |close−100|`
  and ascending-width rank equals index order. A scattered 3-instrument set is left without history,
  exercising the honest partial-universe path (205 ranked / 3 unavailable). It is explicitly
  synthetic validation data, never represented as real market data.
- **OH5 — No Dhan, no network.** `market_provider_enabled=true` selects the enabled composition
  path, but the injected fixture fully replaces the Dhan adapter, so the placeholder access token is
  never read or transmitted. No socket is opened.
- **OH6 — No authority change.** The harness changes no readiness, calendar, ranking, or authority
  semantics; it uses the real packaged NSE 2026 calendar dataset and the real strategy/scanner code.
  It cannot enable current-day authority or any invariant the production path forbids.
- **OH7 — No new dependencies, persistence, WebSocket, or PostgreSQL.** REST only; in-memory only.
- **OH8 — Run command.** `uvicorn app.offline:app` (host/port as usual). Point the frontend at it
  via `VITE_API_BASE_URL=http://localhost:8000/api/v1`.

## Consequences

- Local UI acceptance can reach FULL PASS offline: a real, non-null Narrow CPR snapshot is produced
  end-to-end and consumed by the real frontend.
- The production composition, its mandatory DB/Redis gating, and its Dhan requirement are untouched
  and un-weakened.
- The harness is a validation tool; it is not a runtime mode for any deployed environment.
