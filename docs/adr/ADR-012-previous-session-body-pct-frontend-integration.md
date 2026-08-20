# ADR-012 Addendum — Previous Session Body % Frontend Integration (V1)

| Field | Value |
|-------|-------|
| **Type** | Subordinate governance addendum (not a numbered ADR) |
| **Subordinate to** | ADR-012 — Cross-Instrument Strategy Scanner |
| **Conforms to** | `04_FRONTEND_ARCHITECTURE.md`; reuses ADR-012 Narrow CPR + Previous Session Range % frontend integration infrastructure |
| **Related** | ADR-007 Previous Session Body % strategy specification (PSB1-27), ADR-012 scanner REST API addendum, ADR-013 |
| **Status** | Accepted (frontend integration contract); **implementation DEFERRED** to PREVIOUS-SESSION-BODY-PCT-FRONTEND-IMPL-R1 |
| **Date** | 2026-08-20 |
| **Deciders** | Frontend / Platform Architecture |
| **Decision** | Surface the `previous_session_body_pct` scanner in the React dashboard as a **thin** addition (one presentation object + page + route + nav entry), reusing the existing generic scanner client/hook/table/panel and the generic `formatPercent` **unchanged**. Backend rank order (DESCENDING) is authoritative; the frontend never calculates body % nor re-ranks. |

---

## Context

The generic scanner frontend already serves two opposite-direction strategies unchanged — Narrow CPR (ASCENDING) and Previous Session Range % (DESCENDING) — proving it is ranking-direction agnostic: `ScannerTable` renders candidates in backend order with `defaultColDef.sortable=false` and no `strategyId` branch; `ScannerPresentation` is `{title, subtitle, metricLabel, formatMetric}` with **no** ordering field; `formatPercent` is generic. The PSB backend endpoint was verified server-side (PREVIOUS-SESSION-BODY-PCT-IMPL-R1 E2E): `GET /api/v1/scanners/previous_session_body_pct` → 200, `ranking_metric_name="previous_body_pct"`, `ranking_metric_value` an exact Decimal string, DESCENDING, PARTIAL/COMPLETE. No generic-infrastructure or backend change is required. PSB is DESCENDING like PSR, so the PSR frontend integration is the exact template.

## Governance decisions FE-PSB1–FE-PSB24

- **FE-PSB1 — Route.** `/scanners/previous-session-body-pct`, child of `DashboardLayout`, via the existing data router. No backend route added.
- **FE-PSB2 — Strategy identity.** `strategyId = "previous_session_body_pct"` passed to the generic `ScannerPanel`. No `getPreviousSessionBodyPctScanner()` / `usePreviousSessionBodyPctScanner()`.
- **FE-PSB3 — Title / subtitle.** Title: "Previous Session Body %". Subtitle: "Stocks ranked by the absolute body size of the previous completed session as a percentage of its open. A larger previous-session body % ranks higher." Non-directional, non-predictive.
- **FE-PSB4 — Metric label.** Column header **"Previous Body %"**.
- **FE-PSB5 — Decimal handling.** The exact backend string is retained; display-only formatting to fixed precision (e.g. `"20.800"` → `"20.8000%"`). The frontend never parses the value for ranking/ordering/equality and never reconstructs the metric from `previous_open`/`previous_close`.
- **FE-PSB6 — Backend rank authority.** Candidates render in the exact returned order and rank; no client sort/reverse/renumber/normalize/percentile.
- **FE-PSB7 — Table columns.** Rank, Symbol, Exchange, Previous Body % (+ optional Status if the generic table already shows it). `previous_open`/`previous_close`/`previous_body` are **not** in the REST `ScannerCandidate` projection and are not shown or fabricated. No provider/security fields.
- **FE-PSB8 — COMPLETE UX.** Reuse generic: "Complete", "208 / 208", "Ranked 208".
- **FE-PSB9 — PARTIAL UX.** Reuse generic: "Partial", "205 / 208", "3 instruments were not evaluated"; PARTIAL is normal, not an error.
- **FE-PSB10 — Missing count.** `expected_count − evaluated_count`; identities never listed/inferred.
- **FE-PSB11 — Null snapshot.** "Previous Session Body % scan has not produced results yet." — distinct from empty; never "0 stocks found"; not an error.
- **FE-PSB12 — Empty candidates.** Reuse generic "No ranked candidates in this scan."; counts remain visible; not an error.
- **FE-PSB13 — Errors.** Reuse generic: 404 → deployment/not-available; 503 → "Market scanner is currently unavailable."; network → connectivity state. Distinct from null/empty/partial/complete. No PSB-specific HTTP semantics.
- **FE-PSB14 — Polling.** Reuse existing TanStack Query 15 s foreground-only polling with previous data preserved; no PSB timer/`setInterval`/WebSocket.
- **FE-PSB15 — Manual refresh.** Reuse the generic Refresh/`refetch`; refetches `previous_session_body_pct` at the current limit.
- **FE-PSB16 — Limit.** Reuse the selector (Top 20 / Top 50 / All), default 50; `?limit=N`; counts stay full-snapshot; no local re-ranking/renumbering.
- **FE-PSB17 — Trading date.** Reuse backend `trading_date` + the existing tz-safe formatter; never browser-derived; no invented timestamp.
- **FE-PSB18 — Presentation metadata.** Add exactly one `ScannerPresentation` object (`previousSessionBodyPctPresentation`) — title, subtitle, metric label, `formatMetric`. No ranking-direction knowledge (the panel/table stay direction-agnostic).
- **FE-PSB19 — Responsive & accessibility.** Reuse existing scanner responsive/a11y behavior (keyboard-accessible Refresh, labelled limit selector, semantic grid headers, text-based status, live-region loading/refresh). No new design system.
- **FE-PSB20 — Provider neutrality.** Rendered identity is `exchange` + `symbol` only; no Dhan/`security_id`/`exchange_segment`/token/PIN/TOTP/provider OHLC.
- **FE-PSB21 — Directionality prohibition.** No BUY/SELL/LONG/SHORT/BULLISH/BEARISH/entry/exit/target/stop-loss/signal wording, fields, badges, or icons. The metric is `|close−open|` — the sign is intentionally discarded; rank 1 = largest **absolute** body %, which may come from an up **or** down session. The frontend must preserve that neutrality.
- **FE-PSB22 — Frozen-strategy isolation.** Narrow CPR (ASCENDING) and Previous Session Range % (DESCENDING) stay byte-identical; the generic infra must serve all three with **no** ranking branch (`if strategyId === …` forbidden).
- **FE-PSB23 — No PG/WS/persistence; server state.** PostgreSQL/Redis/WebSocket/persistence not required; snapshot stays TanStack Query server state (not Zustand/Redux/localStorage).
- **FE-PSB24 — Current-session authority isolation.** PSB needs no current-session authority; `staged_observation_verified`/`tick_aggregate_verified`/`supports_current_day` remain False; Open=High/Low unrelated/blocked.

## Formatter decision

`formatPercent` (introduced in the PSR frontend slice) is already generic and correct for a percentage metric — **reuse it unchanged**. No new formatter and no `formatCprWidthPct`-style delegation is needed for PSB.

## Future implementation files (deferred)

Created: one `previousSessionBodyPctPresentation` object in `components/scanners/scannerPresentation.ts`, `pages/PreviousSessionBodyPctScannerPage.tsx`, targeted tests. Modified: `routes/router.tsx` (+1 route), `components/common/Sidebar.tsx` (+1 nav entry). Unchanged: `utils/scannerFormat.ts`, `apiClient.ts`, `scannerClient.ts`, `useScannerSnapshot.ts`, `ScannerTable.tsx`, `ScannerStatusBar.tsx`, `ScannerPanel.tsx`, and all backend.

## Consequences

- A third scanner page ships as metadata + page + route + nav, with zero generic-infrastructure, formatter, or backend change and no ranking branch.
- Narrow CPR V1 and Previous Session Range % V1 stay frozen and byte-identical.
- Implementation deferred to PREVIOUS-SESSION-BODY-PCT-FRONTEND-IMPL-R1.
