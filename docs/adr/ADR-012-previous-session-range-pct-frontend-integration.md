# ADR-012 Addendum — Previous Session Range % Frontend Integration (V1)

| Field | Value |
|-------|-------|
| **Type** | Subordinate governance addendum (not a numbered ADR) |
| **Subordinate to** | ADR-012 — Cross-Instrument Strategy Scanner |
| **Conforms to** | `04_FRONTEND_ARCHITECTURE.md`; reuses ADR-012 Narrow CPR frontend integration infrastructure |
| **Related** | ADR-007 Previous Session Range % strategy specification (PSR1-24), ADR-012 scanner REST API addendum, ADR-013 |
| **Status** | Accepted (frontend integration contract); **implementation DEFERRED** to PREVIOUS-SESSION-RANGE-PCT-FRONTEND-IMPL-R1 |
| **Date** | 2026-08-19 |
| **Deciders** | Frontend / Platform Architecture |
| **Decision** | Surface the `previous_session_range_pct` scanner in the React dashboard as a **thin** addition (presentation metadata + page + route + nav entry), reusing the existing generic scanner client/hook/table/panel unchanged. Backend rank order (DESCENDING) is authoritative; the frontend never calculates or re-ranks. |

---

## Context

The generic scanner frontend (built for Narrow CPR) is ranking-direction agnostic: `ScannerTable` renders candidates in backend order with `defaultColDef.sortable=false` (never re-sorts), and `ScannerPresentation` is `{title, subtitle, metricLabel, formatMetric}` with **no** ordering field. The backend endpoint `GET /api/v1/scanners/{strategy_id}` is path-generic and already serves `previous_session_range_pct` (verified server-side: `ranking_metric_name="previous_range_pct"`, `ranking_metric_value` exact Decimal string, DESCENDING order, PARTIAL/COMPLETE). No generic-infrastructure change is required.

## Governance decisions FE-PSR1–FE-PSR24

- **FE-PSR1 — Route.** `/scanners/previous-session-range-pct`, child of `DashboardLayout`, via the existing data router. No backend route added.
- **FE-PSR2 — Strategy identity.** `strategyId = "previous_session_range_pct"` passed to the generic `ScannerPanel`. No `getPreviousSessionRangePctScanner()` / `usePreviousSessionRangePctScanner()`.
- **FE-PSR3 — Title / subtitle.** Title: "Previous Session Range %". Subtitle: "Stocks ranked by the previous completed session's high-low range as a percentage of its open. A larger previous-session range % ranks higher." Non-directional, non-predictive.
- **FE-PSR4 — Metric label.** Column header **"Previous Range %"** (concise for the table; the full name is the page title).
- **FE-PSR5 — Decimal handling.** The exact backend string is retained; display-only formatting to fixed precision (e.g. `"20.800"` → `"20.8000%"`). The frontend never parses the value for ranking/ordering/equality.
- **FE-PSR6 — Backend rank authority.** Candidates render in the exact returned order and rank; no client re-sort/reverse/renumber.
- **FE-PSR7 — Table columns.** Rank, Symbol, Exchange, Previous Range % (+ optional Status if the generic table already shows it). `previous_open/high/low/close` are **not** in the REST `ScannerCandidate` projection and are not shown or fabricated. No provider/security fields.
- **FE-PSR8 — COMPLETE UX.** Reuse generic: "Complete", "208 / 208", "Ranked 208".
- **FE-PSR9 — PARTIAL UX.** Reuse generic: "Partial", "205 / 208", "3 instruments were not evaluated"; PARTIAL is normal, not an error.
- **FE-PSR10 — Missing count.** `expected_count − evaluated_count`; identities never listed/inferred (backend does not expose them).
- **FE-PSR11 — Null snapshot.** "Previous Session Range % scan has not produced results yet." — distinct from empty; never "0 stocks found"; not an error.
- **FE-PSR12 — Empty candidates.** Reuse generic "No ranked candidates in this scan."; counts remain visible; not a server/network error.
- **FE-PSR13 — Errors.** Reuse generic: 404 → deployment/not-available state; 503 → "Market scanner is currently unavailable."; network → connectivity state. Distinct from null/empty/partial/complete. No PSR-specific HTTP semantics.
- **FE-PSR14 — Polling.** Reuse existing TanStack Query 15 s foreground-only polling with previous data preserved; no PSR timer, no `setInterval`, no WebSocket.
- **FE-PSR15 — Manual refresh.** Reuse the generic Refresh button / `refetch`; only the strategy id differs.
- **FE-PSR16 — Limit.** Reuse the existing selector (Top 20 / Top 50 / All), default 50; `?limit=20` etc.; counts stay full-snapshot; no local re-ranking or renumbering.
- **FE-PSR17 — Trading date.** Reuse the backend `trading_date` + the existing tz-safe formatter; never derive from the browser clock; no invented timestamp.
- **FE-PSR18 — Generic presentation reuse.** Add exactly one `ScannerPresentation` object (`previousSessionRangePctPresentation`) — title, subtitle, metric label, formatter. No ranking-direction knowledge in the frontend (the panel/table stay direction-agnostic).
- **FE-PSR19 — Responsive & accessibility.** Reuse existing scanner responsive/a11y behavior (keyboard-accessible Refresh, labelled limit selector, semantic grid headers, text-based status, live-region loading/refresh). No new design system.
- **FE-PSR20 — Provider neutrality.** Rendered identity is `exchange` + `symbol` only; no Dhan/`security_id`/`exchange_segment`/token/PIN/TOTP/provider OHLC.
- **FE-PSR21 — Directionality prohibition.** No BUY/SELL/LONG/SHORT/BULLISH/BEARISH/ENTRY/EXIT/TARGET/STOP-LOSS/signal wording, fields, badges, or icons. Rank 1 = largest previous-session range %, nothing more.
- **FE-PSR22 — Narrow CPR isolation.** Narrow CPR title/subtitle/label ("CPR Width %")/route/ASCENDING rendering/rank-1/polling/limit/UX stay unchanged; the generic infra must serve both with **no** ranking branch (`if strategyId === …` forbidden).
- **FE-PSR23 — No PG/WS/persistence.** PostgreSQL not required; no new Redis requirement; no WebSocket; no persistence; snapshot stays TanStack Query server state (not Zustand/Redux/localStorage).
- **FE-PSR24 — Current-session authority isolation.** PSR needs no current-session authority; `staged_observation_verified`/`tick_aggregate_verified`/`supports_current_day` remain False; Open=High/Low stays blocked and unrelated.

## Formatter decision

The current `formatCprWidthPct` is a generic fixed-precision percent formatter with a CPR-specific name. IMPL should **extract a generic `formatPercent(raw)`** into `utils/scannerFormat.ts` (identical logic: empty/malformed guard, `toFixed(4)`, `%` suffix), have `formatCprWidthPct` delegate to it (behavior-identical, so Narrow CPR output is unchanged and regression-tested), and set the PSR presentation's `formatMetric` to `formatPercent`. This avoids both a CPR-named formatter on a non-CPR metric and duplicated logic — the smallest change that keeps Narrow CPR byte-identical.

## Future implementation files (deferred)

Created: `components/scanners/scannerPresentation.ts` (+ one `previousSessionRangePctPresentation` object), `pages/PreviousSessionRangePctScannerPage.tsx`, targeted tests. Modified: `routes/router.tsx` (+1 route), `components/common/Sidebar.tsx` (+1 nav entry), `utils/scannerFormat.ts` (extract `formatPercent`; CPR delegates). Unchanged: `apiClient.ts`, `scannerClient.ts`, `useScannerSnapshot.ts`, `ScannerTable.tsx`, `ScannerStatusBar.tsx`, `ScannerPanel.tsx`, and all backend.

## Consequences

- A second scanner page ships as metadata + page + route + nav, with zero generic-infrastructure or backend change and no ranking branch.
- Narrow CPR V1 stays frozen and byte-identical.
- Implementation deferred to PREVIOUS-SESSION-RANGE-PCT-FRONTEND-IMPL-R1.
