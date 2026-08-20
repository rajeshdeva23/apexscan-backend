# ADR-012 Addendum — Narrow CPR Scanner Frontend Integration (V1)

| Field | Value |
|-------|-------|
| **Type** | Subordinate governance addendum (not a numbered ADR) |
| **Subordinate to** | ADR-012 — Cross-Instrument Strategy Scanner |
| **Conforms to** | `04_FRONTEND_ARCHITECTURE.md` (authoritative Frontend Architecture Specification) |
| **Related** | ADR-012 scanner REST API addendum (the transport this UI consumes), ADR-007 Narrow CPR strategy specification, ADR-007 partial-universe historical readiness, ADR-013 (strategy registration/enablement), ADR-002 (two-repository ownership) |
| **Status** | Accepted (frontend integration contract); **implementation DEFERRED** to FRONTEND-NARROW-CPR-INTEGRATION-IMPL-R1 |
| **Date** | 2026-08-18 |
| **Deciders** | Frontend / Platform Architecture |
| **Decision** | Render the Narrow CPR scanner snapshot in the React dashboard through the existing generic REST endpoint, using REST polling (no WebSocket), a **generic** scanner client/hook/table reused by future scanners, backend-authoritative rank order, honest PARTIAL/COMPLETE and missing-count presentation, and strictly non-directional language. No backend change is required. |

---

## Context

Narrow CPR backend V1 is frozen and validated end-to-end (1422 passed / 9 skipped). The scanner
exposes exactly one read-only transport (ADR-012 scanner REST API addendum):

```
GET /api/v1/scanners/{strategy_id}         # narrow_cpr
GET /api/v1/scanners/{strategy_id}?limit=N # 1..500 top-N projection over existing rank order
```

Verified response contract (from `app/schemas/scanner.py`, not paraphrase):

```
ScannerResponse            { snapshot: ScannerSnapshotResponse | null }
ScannerSnapshotResponse    { strategy_id, strategy_version, config_version,
                             trading_date (ISO date string, e.g. "2026-08-06"),
                             expected_count, evaluated_count, eligible_count,
                             completeness ("partial" | "complete"),
                             candidates: ScannerCandidateResponse[] }
ScannerCandidateResponse   { rank, exchange, symbol, status ("matched"),
                             ranking_metric_name ("cpr_width_pct"),
                             ranking_metric_value (exact Decimal STRING, e.g. "0.03125") }
```

Verified HTTP semantics: `200` with a snapshot; `200` with `snapshot: null` (strategy is
scanner-enabled but has produced no snapshot yet); `404` when `strategy_id` is not scanner-enabled;
`503` when the scanner runtime is unavailable/not started. Every response is `Cache-Control:
no-store`. The REST GET performs **zero** provider/historical/evaluation work; `limit` truncates the
emitted candidate list only and never alters `expected_count` / `evaluated_count` / `eligible_count`
or ranking. The backend never fabricates results and does **not** currently expose the identities of
un-evaluated instruments or a snapshot generation timestamp.

Frontend repository inspection (`apexscan-frontend`):

- **Stack:** React 19, TypeScript 5.7 (strict), Vite 6, `react-router` 8.3.0 (data router; no
  `react-router-dom`), `@tanstack/react-query` 5 (server state), `zustand` 5 (client/UI state),
  `ag-grid-react`/`ag-grid-community` 33 (dense grids), `lightweight-charts` 5, Tailwind v4.
- **Architecture:** `04_FRONTEND_ARCHITECTURE.md` is authoritative. The layout is **layer-by-concern**
  (`components/`, `pages/`, `layouts/`, `hooks/`, `services/`, `store/`, `routes/`, `types/`,
  `utils/`, `assets/`, `styles/`, target `grid/` and `charts/`) — **not** a `src/features/*` module
  layout. The Dependency Rule: only `services/` touches the network; only `hooks/` call `services/`;
  components/pages read data from hooks and UI state from the store.
- **Present state:** a Phase-1 infrastructure shell. `services/apiClient.ts` (`apiRequest<T>` over
  `fetch`, base URL from `import.meta.env.VITE_API_BASE_URL`), one example hook `useHealth`
  (TanStack Query template), an empty `types/`, a minimal `zustand` store (sidebar), a data router
  with `Home`/`Dashboard`, and a `DashboardLayout` (sidebar + header). No scanner code exists.
- **No test framework is installed** (`npm run test --if-present` tolerates its absence). Adding one
  is an implementation-phase prerequisite (FE-NCR30).

This addendum governs the frontend integration only. It changes no backend semantics, adds no
transport to the backend, and does not alter `04_FRONTEND_ARCHITECTURE.md`.

## Governance decisions FE-NCR1–FE-NCR31

- **FE-NCR1 — Conform to the existing concern-based architecture; reject the feature-folder layout.**
  Scanner code is placed across the existing `04_FRONTEND_ARCHITECTURE.md` folders — `services/`
  (client), `hooks/` (query hook), `types/` (contract), `grid/` (AG Grid table + column defs),
  `components/` (feature panel), `pages/` (route view), `routes/` (route table). A `src/features/
  scanners/…` module tree is **not** introduced: it conflicts with the authoritative architecture and
  is unnecessary because a generic scanner surface is achievable within the current layout.

- **FE-NCR2 — Generic scanner API client (not Narrow-CPR-specific).** Add
  `getScannerSnapshot(strategyId: string, options?: { limit?: number }): Promise<ScannerResponse>` to
  `services/`, built on the existing `apiRequest`. It targets `/scanners/${strategyId}` with an
  optional `limit` query. No `getNarrowCprScanner()`; future scanners (`open_high`, `open_low`,
  `momentum`, …) reuse the same client with a different `strategyId`.

- **FE-NCR3 — Structured error normalisation in `services/` (frontend-only).** The current
  `apiRequest` throws a generic `Error` on non-OK responses, discarding the status code, so hooks
  cannot distinguish `404` from `503`. IMPL extends the client to throw a typed `ApiError { status,
  message }` (and a distinct network/transport failure), per `04_FRONTEND_ARCHITECTURE.md` §6.4. This
  is a `services/`-layer enhancement, not a new architectural decision and not a backend change.

- **FE-NCR4 — Generic server-state hook.** Add `useScannerSnapshot(strategyId, { limit, refetchInterval
  })` in `hooks/`, wrapping TanStack Query with query key `['scanner', strategyId, { limit }]`.
  Components never call `services/` directly (Dependency Rule).

- **FE-NCR5 — Response typing mirrors the backend exactly.** Add to `types/` the shapes
  `ScannerResponse`, `ScannerSnapshot`, `ScannerCandidate` with backend field names and casing
  verbatim (`snapshot`, `strategy_id`, `strategy_version`, `config_version`, `trading_date`,
  `expected_count`, `evaluated_count`, `eligible_count`, `completeness`, `candidates`, `rank`,
  `exchange`, `symbol`, `status`, `ranking_metric_name`, `ranking_metric_value`). `snapshot` is
  nullable. `completeness` is the string union `'partial' | 'complete'`. `ranking_metric_value`
  stays `string`. No invented fields; no camelCase remap.

- **FE-NCR6 — Transport: REST polling only; WebSocket deferred.** V1 uses REST. The backend exposes
  no scanner WebSocket (ADR-012 REST addendum API1; `app/websocket/__init__.py` is an empty
  placeholder). See DEVIATIONS: `04_FRONTEND_ARCHITECTURE.md` §6.2/§6.6 describe an eventual
  WebSocket push where "the UI does not poll"; V1 polls because the push channel does not yet exist.
  The `services/`→`hooks/` seam (§6.8) lets a later scanner WS channel replace polling without
  touching components.

- **FE-NCR7 — Polling model: TanStack Query `refetchInterval`, foreground only.** Poll every **15 s**
  while the page is visible; set `refetchIntervalInBackground: false` so hidden tabs do not poll. The
  snapshot changes only when new material strategy results arrive, so 15 s is a deliberate,
  non-aggressive cadence, not real-time push. The interval is a single governed constant.

- **FE-NCR8 — Manual refresh in addition to polling.** Provide a keyboard-accessible refresh control
  that calls the query's `refetch`. TanStack Query deduplicates in-flight requests, so repeated
  clicks do not create overlapping uncontrolled fetches.

- **FE-NCR9 — Background refresh preserves the current snapshot.** Use `placeholderData:
  keepPreviousData` so a background poll never blanks the table or flashes a full-page spinner; show a
  subtle "refreshing" affordance instead. A full skeleton appears only on the first load.

- **FE-NCR10 — Query limit default `50`; counts always from the full snapshot.** The dashboard
  requests `?limit=50` for payload economy. Because `limit` truncates candidates only, the displayed
  `expected_count` / `evaluated_count` / `eligible_count` remain the full-universe values. When
  candidates are truncated, the UI states it honestly (e.g. "Ranked: 205 · showing top 50") and
  offers an explicit "Show all" action (raises/removes `limit`, ≤ 500). Limit is response projection
  only and never re-ranks.

- **FE-NCR11 — Decimal handling: keep the raw string; format for display only.** `ranking_metric_value`
  is retained as the exact backend string. A pure `utils/` formatter renders it for display (fixed
  precision, e.g. `"0.0313%"`). The raw string remains available (e.g. as a cell tooltip / data
  attribute). The parsed numeric form is used for **display only** — never for ranking, ordering,
  equality, or persistence.

- **FE-NCR12 — Metric display formatting.** Label the column "CPR Width %". Format
  `ranking_metric_value` to a deliberate fixed precision for readability; do not imply more precision
  than shown, and keep the exact value reachable. Formatting is presentation, not computation.

- **FE-NCR13 — Rank order is backend-authoritative.** Candidates render in the exact order returned
  (rank ascending; rank 1 = narrowest CPR). The frontend never re-sorts by string value, locale,
  symbol, or percentage. AG Grid client-side column sorting stays **off by default** in V1; any future
  user-chosen sort is an explicit, reversible display mode that never becomes the default.

- **FE-NCR14 — Primary table columns.** V1 columns: **Rank, Symbol, Exchange, CPR Width %** (optional
  **Status**). `strategy_version`, `config_version`, and `ranking_metric_name` are not shown by
  default; they may appear in a details/debug affordance.

- **FE-NCR15 — Neutral, non-directional labelling.** Title "Narrow CPR"; subtitle e.g. "Stocks ranked
  by previous-session CPR width. Smaller CPR width ranks higher." Forbidden anywhere in the UI: "best
  stocks", "buy", "sell", "bullish", "bearish", "breakout", "high-probability trade", or any
  BUY/SELL/direction field. Narrow CPR is a structural scanner, not a signal.

- **FE-NCR16 — Completeness is always visible.** Render a `PARTIAL` / `COMPLETE` badge plus counts —
  "Scanned: 205 / 208 · Ranked: 205 · Status: Partial" — using backend counts verbatim. PARTIAL is a
  normal outcome and is **not** rendered as an error.

- **FE-NCR17 — Missing-count is honest and derived.** Show "N instruments were not evaluated" where
  `N = expected_count − evaluated_count`. The UI never lists missing symbol identities (the backend
  does not expose them) and never infers them. Exposing identities would require a new backend
  contract — OUT OF SCOPE and not needed for V1.

- **FE-NCR18 — Null snapshot UX (distinct from empty).** `snapshot: null` renders "Narrow CPR scan has
  not produced results yet." — never "0 stocks found". A `200` with a snapshot whose candidate list is
  empty is a different, separately-handled state.

- **FE-NCR19 — 404 UX (deployment/config error).** For the Narrow CPR page a `404` means the strategy
  is not scanner-enabled — a deployment/configuration problem. Render a clear, distinct error state;
  never a silent empty table.

- **FE-NCR20 — 503 UX (runtime unavailable).** Render "Market scanner is currently unavailable." — a
  distinct state, never conflated with "no Narrow CPR stocks." `503` is retryable through ordinary
  polling.

- **FE-NCR21 — Network-error UX.** Distinguish transport failure from `404`, `503`, null snapshot,
  empty candidates, PARTIAL, and COMPLETE. Show a connectivity message with retry; do not collapse
  everything into "No data."

- **FE-NCR22 — Loading state.** First load shows the existing skeleton/placeholder convention; the UI
  never renders against undefined data (§6.6). Background polling keeps prior data (FE-NCR9).

- **FE-NCR23 — Trading date display.** Display backend `trading_date` (ISO date string) formatted via
  locale, labelled "Trading date:" (e.g. "Trading date: 06 Aug 2026"). Never derive the trading date
  from the browser clock.

- **FE-NCR24 — No fabricated market timestamp.** The snapshot has no generation timestamp; V1 omits a
  "last updated" line rather than invent one. If a local fetch time is ever shown, it is labelled
  "Fetched at" (local), never "Market updated at". A true market-data timestamp would require a new
  backend field — OUT OF SCOPE.

- **FE-NCR25 — Generic `ScannerTable` component.** Build a `ScannerTable` (under `grid/`, AG Grid)
  generic over `ScannerSnapshot`, parameterised by presentation metadata (title, subtitle, metric
  column label, metric formatter). The Narrow CPR page supplies its metadata. Future scanners reuse
  the same table. This is justified now (a concrete second consumer is on the roadmap) and does not
  over-generalise beyond a single metric column.

- **FE-NCR26 — Feature location (concern-based).** `services/scannerClient.ts`,
  `hooks/useScannerSnapshot.ts`, `types/scanner.ts`, `grid/ScannerTable.tsx` (+ column defs),
  `components/` scanner panel, `pages/NarrowCprScannerPage.tsx`, route entry in `routes/`. Exact
  filenames are an IMPL detail; the placement obeys FE-NCR1.

- **FE-NCR27 — Route.** `/scanners/narrow-cpr` (plural, matching the generic REST collection and
  future scanners), registered as a child of `DashboardLayout` in the existing data router. No backend
  route is added.

- **FE-NCR28 — Navigation.** Add a "Scanners" area to the existing sidebar with a "Narrow CPR" entry,
  following the current `NavLink` pattern. The dashboard is not redesigned.

- **FE-NCR29 — Responsive & accessible.** Desktop: AG Grid table. Small screens: the existing
  responsive convention (stacked/card equivalent) — no new design system. Accessibility: semantic
  table/grid headers, status conveyed by text (not colour alone), a keyboard-accessible refresh
  control, and loading/refresh announced via the existing live-region convention.

- **FE-NCR30 — State management.** Snapshot data is **server state** → TanStack Query only; it is not
  placed in `zustand` or Redux. `zustand`/local component state or URL query params hold only UI
  preferences (e.g. limit / show-all). API base URL is consumed solely through the existing
  `services/` client (`import.meta.env.VITE_API_BASE_URL`); no host/port/provider URL is hardcoded.

- **FE-NCR31 — Security, retry, and independence.** The scanner endpoint is unauthenticated, so this
  slice adds no auth mechanism and handles no Dhan credentials / PIN / TOTP / access token /
  `security_id`; nothing secret enters the bundle. Retry policy: never retry `404`; `503` recovers via
  ordinary polling; transient network errors use bounded retry/backoff (§6.5) — no infinite tight
  retries. The feature depends on neither PostgreSQL (the endpoint is in-memory/current-state) nor
  WebSocket.

## Implementation test matrix (deferred to FRONTEND-NARROW-CPR-INTEGRATION-IMPL-R1)

Tests mock the backend at the `services/` boundary (§14.1); no live backend/broker. Cover: (A) loading
state; (B) COMPLETE response; (C) PARTIAL response; (D) evaluated/expected counts displayed; (E)
candidate rank order preserved; (F) narrowest at rank 1 first; (G) Decimal display formatting with raw
string retained; (H) null snapshot ("not produced results yet"); (I) 404 error state; (J) 503
unavailable state; (K) network-error state; (L) `limit` query sent correctly; (M) no client-side
re-ranking; (N) trading-date display from backend value; (O) no directional language present; (P) no
BUY/SELL/direction fields rendered; (Q) no provider IDs / `security_id`; (R) manual refresh; (S)
polling cadence + foreground-only; (T) background refresh preserves current data; (U) responsive /
semantic table; (V) generic `ScannerTable` works against a second-scanner fixture; (W) API base URL via
existing env config; (X) no PostgreSQL/WebSocket dependency; (Y) lint / typecheck / build clean; (Z) no
regression to existing shell. A test framework (e.g. Vitest + Testing Library) is installed as an IMPL
prerequisite (FE-NCR30 note).

## Backend impact

**None.** No change to `NarrowCprStrategy`, `RequirementsCoordinator`, `HistoricalWarmupService`,
`CrossInstrumentStrategyScanner`, the scanner REST endpoint, the calendar dataset, provider adapters,
runtime composition, or authority/readiness flags. All V1 UX is met by the existing contract. Two
future capabilities would each require a **new** backend contract and are explicitly deferred, not
worked around: (1) identities of un-evaluated instruments; (2) an authoritative snapshot generation
timestamp.

## Consequences

- The dashboard renders honest, backend-authoritative Narrow CPR results with explicit
  PARTIAL/COMPLETE and missing-count semantics and no directional framing.
- A single generic client/hook/table serves every future scanner, so subsequent scanner UIs are page +
  metadata, not new plumbing.
- V1 polls; the seam preserves a clean later migration to WebSocket push when the backend adds a
  scanner channel.
- Implementation is deferred to FRONTEND-NARROW-CPR-INTEGRATION-IMPL-R1.
