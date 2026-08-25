# ADR-012 Addendum — Previous Session Relative Range Frontend Integration (V1)

| Field | Value |
|-------|-------|
| **Type** | Subordinate governance addendum (not a numbered ADR) |
| **Subordinate to** | ADR-012 — Cross-Instrument Strategy Scanner |
| **Conforms to** | `04_FRONTEND_ARCHITECTURE.md`; reuses ADR-012 Narrow CPR + Previous Session Range % + Previous Session Body % frontend integration infrastructure |
| **Related** | ADR-007 Previous Session Relative Range strategy specification (PSRR1-30), ADR-007 multi-session historical lookback capability (MSH1-16), ADR-012 scanner REST API addendum, ADR-013, FRONTEND-V1-DESIGN-REFERENCE-GOV-R1 |
| **Status** | Accepted (frontend integration contract); **implementation DEFERRED** to PREVIOUS-SESSION-RELATIVE-RANGE-FRONTEND-IMPL-R1 |
| **Date** | 2026-08-24 |
| **Deciders** | Frontend / Platform Architecture |
| **Decision** | Surface the `previous_session_relative_range` scanner in the React dashboard as a **thin** addition (one presentation object + page + route + nav entry) reusing the existing generic scanner client/hook/table/panel unchanged, plus the **one** genuinely new piece the metric requires: a dedicated ratio formatter (`formatRatioMultiple`) rendering the dimensionless `relative_range_ratio` as `0.42×`. Backend rank order (ASCENDING) is authoritative; the frontend never computes the ratio nor re-ranks. |

---

## Context

The generic scanner frontend already serves three strategies unchanged — Narrow CPR (ASCENDING), Previous Session Range % (DESCENDING), Previous Session Body % (DESCENDING) — proving it is ranking-direction agnostic: `ScannerTable` renders candidates in backend order with `defaultColDef.sortable=false` and no `strategyId` branch; `ScannerPresentation` is `{title, subtitle, metricLabel, formatMetric}` with **no** ordering field. The PSRR backend endpoint is governed (ADR-007 PSRR spec) and registered on `main`: `GET /api/v1/scanners/previous_session_relative_range` → `ranking_metric_name="relative_range_ratio"`, `ranking_metric_value` an exact Decimal string, ASCENDING, PARTIAL/COMPLETE. No generic-infrastructure or backend change is required.

**What makes PSRR different from the three frozen scanners — and the only reason this is not a byte-for-byte PSB clone:** its ranking metric is a **dimensionless ratio**, not a percentage. `formatPercent` (fixed-4 `toFixed` + `%`) is semantically wrong for it — a ratio of `0.5` is not `0.5000%`. PSRR therefore introduces exactly one new display-only formatter, `formatRatioMultiple`, and reuses **everything else** (client, hook, table, panel, status bar, polling, limit, error/empty/null handling, responsive/a11y) unchanged. It is also the first **multi-session** scanner surfaced (backend `HistoricalRequirement(session, 21)`), which changes only the *likelihood* of PARTIAL, not the frontend's handling of it.

This slice adopts the user-confirmed Frontend V1 decisions (single scanner-per-route IA; search / logos / market-clock deferred; the dark dashboard as the evolvable visual baseline) — none of which are implemented here; this artifact governs only the PSRR data/contract surface.

## Governance decisions FE-PSRR1–FE-PSRR31

- **FE-PSRR1 — Route.** `/scanners/previous-session-relative-range`, child of `DashboardLayout`, via the existing data router. No backend route added. (User-confirmed.)
- **FE-PSRR2 — Strategy identity.** `strategyId = "previous_session_relative_range"` passed to the generic `ScannerPanel`. No `getPreviousSessionRelativeRangeScanner()` / `usePreviousSessionRelativeRangeScanner()`; the generic `scannerClient` / `useScannerSnapshot` are used as-is.
- **FE-PSRR3 — Title.** Page title / navigation label: **"Previous Session Relative Range"**. (User-confirmed.) Non-directional, non-predictive.
- **FE-PSRR4 — Subtitle.** "Stocks ranked by how compressed the previous completed session's range was relative to that instrument's own 20-session median range (previous-session range % ÷ the median range % of the prior 20 sessions). A smaller ratio ranks higher — the previous session was more compressed versus its own recent baseline." Describes structure only; no trade direction, recommendation, breakout implication, or probability.
- **FE-PSRR5 — Metric label.** Column header **"Relative Range"**. (User-confirmed.) The full name is the page title; the unit/meaning is carried by the `×` display form (FE-PSRR8) and the subtitle.
- **FE-PSRR6 — Ranking-metric name.** Backend `ranking_metric_name = "relative_range_ratio"`; the frontend reads the generic `ranking_metric_value` string for this metric and never hard-codes a per-strategy field name in the table.
- **FE-PSRR7 — Ordering authority.** Backend ordering is **ASCENDING** and authoritative: rank 1 = smallest `relative_range_ratio` = most compressed vs its own baseline. The frontend carries **no** ordering knowledge (no ordering field in the presentation object) and performs no client sort/reverse.
- **FE-PSRR8 — Ratio display form (the one new formatter).** `relative_range_ratio` is a **dimensionless ratio, not a percentage**, so `formatPercent` MUST NOT be used. IMPL adds a dedicated generic display-only formatter `formatRatioMultiple(raw)` to `utils/scannerFormat.ts` that renders a fixed-2-decimal value with a trailing `×` (U+00D7 MULTIPLICATION SIGN): `"0.5"` → `"0.50×"`, `"0.42"` → `"0.42×"`, `"1"` → `"1.00×"`, `"2"` → `"2.00×"`. `0.42×` means the previous session's range was 0.42× (42% of) the instrument's own 20-session median range; `1.00×` = equal to baseline; `> 1.00×` = expansion vs baseline; `0.00×` = maximal compression (a valid rank-1 value). The `×` denotes a **self-relative multiple**, never a cross-instrument or absolute quantity. Governed formatter shape (mirrors `formatPercent`'s guards):

  ```ts
  export const RATIO_DISPLAY_DECIMALS = 2;

  export function formatRatioMultiple(raw: string): string {
    if (raw.trim() === '') {
      return raw; // never invent a value
    }
    const value = Number(raw);
    if (!Number.isFinite(value)) {
      return raw; // malformed → exact backend string shown unchanged
    }
    return `${value.toFixed(RATIO_DISPLAY_DECIMALS)}×`;
  }
  ```

- **FE-PSRR9 — Display precision is presentation-only.** `RATIO_DISPLAY_DECIMALS = 2` is a display constant (user-confirmed `0.42×` style); it is user-adjustable later without contract impact. Two-decimal rounding MAY render distinct raw ratios identically (e.g. `0.418` and `0.423` both show `0.42×`; a small nonzero and exact zero may both show `0.00×`). This is acceptable and honest: the exact Decimal string is preserved in the payload, the Rank column disambiguates, and rounding never affects order — identical to how the fixed-4 `formatPercent` already behaves for the three percentage scanners.
- **FE-PSRR10 — Decimal handling.** The exact backend Decimal string is retained by callers; formatting is display-only. The frontend never parses `relative_range_ratio` for ranking/ordering/equality and never reconstructs it from `previous_range_pct` / `baseline_range_pct` (which are not in the REST projection — FE-PSRR12).
- **FE-PSRR11 — Backend rank authority.** Candidates render in the exact returned order and rank; no client re-sort/reverse/renumber/normalize/percentile/invert (`1/ratio`).
- **FE-PSRR12 — Table columns.** Rank, Symbol, Exchange, Relative Range (+ optional Status if the generic table already shows it). The PSRR result's `previous_range_pct`, `baseline_range_pct`, `baseline_sessions` (20), and `source_session_date` are **not** in the REST `ScannerCandidate` projection (`rank / exchange / symbol / status / ranking_metric_name / ranking_metric_value` only) and are **not** shown, derived, or fabricated. No provider/security fields.
- **FE-PSRR13 — COMPLETE UX.** Reuse generic: "Complete", "N / N", "Ranked N".
- **FE-PSRR14 — PARTIAL UX.** Reuse generic: "Partial", "evaluated / expected", "K instruments were not evaluated"; PARTIAL is normal, not an error. **Note (not special-cased):** because PSRR requires 21 authoritative completed sessions, more instruments may be SKIPPED early (insufficient history → `PREVIOUS_SESSION_RELATIVE_RANGE_NO_HISTORY`) or on a degenerate baseline (`…_DEGENERATE_BASELINE`) than for the single-session scanners, so PARTIAL is expected more often. The frontend handling is **identical** — honest counts, no PSRR-specific messaging.
- **FE-PSRR15 — Missing count.** Derived `expected_count − evaluated_count`; identities never listed or inferred. The backend snapshot does not expose per-instrument skip reasons (NO_HISTORY vs DEGENERATE_BASELINE vs local gap), so the frontend must not attempt to distinguish or explain them — only the aggregate count.
- **FE-PSRR16 — Null snapshot.** "Previous Session Relative Range scan has not produced results yet." — distinct from empty; never "0 stocks found"; not an error.
- **FE-PSRR17 — Empty candidates.** Reuse generic "No ranked candidates in this scan."; counts remain visible; not a server/network error.
- **FE-PSRR18 — Errors.** Reuse generic: 404 → deployment/not-available; 503 → "Market scanner is currently unavailable."; network → connectivity state. Distinct from null/empty/partial/complete. No PSRR-specific HTTP semantics.
- **FE-PSRR19 — Polling.** Reuse existing TanStack Query 15 s foreground-only polling with previous data preserved; no PSRR timer/`setInterval`/WebSocket.
- **FE-PSRR20 — Manual refresh.** Reuse the generic Refresh / `refetch`; refetches `previous_session_relative_range` at the current limit.
- **FE-PSRR21 — Limit.** Reuse the selector (Top 20 / Top 50 / All), default 50; `?limit=N` truncates candidates only; counts stay full-snapshot; no local re-ranking/renumbering.
- **FE-PSRR22 — Trading date.** Reuse backend `trading_date` + the existing tz-safe `formatTradingDate`; never browser-derived; no invented timestamp. (The per-candidate `source_session_date` is not in the REST projection; the snapshot-level `trading_date` is displayed, as for every scanner.)
- **FE-PSRR23 — Presentation metadata.** Add exactly one `ScannerPresentation` object (`previousSessionRelativeRangePresentation`) — title, subtitle, `metricLabel: "Relative Range"`, `formatMetric: formatRatioMultiple`. No ranking-direction knowledge (the panel/table stay direction-agnostic).
- **FE-PSRR24 — Responsive & accessibility.** Reuse existing scanner responsive/a11y behavior (keyboard-accessible Refresh, labelled limit selector, semantic grid headers, text-based status, live-region loading/refresh). The `×` is rendered as ordinary metric-cell text (as `%` is today); no new design system, no extra ARIA required.
- **FE-PSRR25 — Provider neutrality.** Rendered identity is `exchange` + `symbol` only; no Dhan/`security_id`/`exchange_segment`/token/PIN/TOTP/provider OHLC.
- **FE-PSRR26 — Directionality prohibition.** No BUY/SELL/LONG/SHORT/BULLISH/BEARISH/entry/exit/target/stop-loss/signal wording, fields, badges, or icons. Rank 1 = the previous session most compressed relative to its own 20-session baseline — a structural volatility-compression observation, **not** a breakout prediction, "coiling", momentum, or directional bias. The frontend must preserve that neutrality in copy and layout.
- **FE-PSRR27 — Frozen-strategy isolation.** Narrow CPR / Previous Session Range % / Previous Session Body % stay byte-identical (title/subtitle/label/route/rendering/rank-1/polling/limit/UX). Adding `formatRatioMultiple` MUST NOT alter `formatPercent` / `formatCprWidthPct` behavior — the three percentage scanners' output stays byte-identical (regression-tested). The generic infra must serve all four with **no** ranking branch (`if strategyId === …` forbidden).
- **FE-PSRR28 — No PG/WS/persistence; server state.** PostgreSQL/Redis/WebSocket/persistence not required; snapshot stays TanStack Query server state (not Zustand/Redux/localStorage).
- **FE-PSRR29 — Current-session authority isolation.** PSRR needs no current-session authority; `staged_observation_verified` / `tick_aggregate_verified` / `supports_current_day` remain False; Open=High/Low unrelated/blocked.
- **FE-PSRR30 — Information architecture.** One scanner per route/page (user-confirmed V1 IA). PSRR is a standalone page reachable from the Scanners nav section; no multi-panel dashboard (deferred, ungoverned), no search, no logos, no market-clock — none introduced by this slice.
- **FE-PSRR31 — Design-system baseline & dependency constraints.** The PSRR page inherits the platform frontend design system and introduces **none of its own**: the existing React + TypeScript + Tailwind architecture, **Material 3 design *principles***, the supplied **StepOne dark dashboard** visual baseline, and **Material Symbols** for icons where appropriate. No new UI dependency — **Bootstrap is prohibited**; **MUI / Material UI is NOT introduced** unless a future requirement specifically justifies it. The generic scanner components are **preserved and restyled via Tailwind design tokens**, never replaced. This is a platform-wide stance recorded here because it binds the PSRR slice; formalizing it across all scanner surfaces belongs to a dedicated frontend design-system governance slice. Consistent with FE-PSRR24 (PSRR adds no bespoke design system) and FE-PSRR28 (server state unchanged).

## Formatter decision

PSRR is the first scanner whose ranking metric is **not** a percentage. `formatPercent` is reused unchanged for CPR/PSR/PSB but is **not** applicable here. IMPL adds one new generic display-only formatter `formatRatioMultiple(raw)` (FE-PSRR8) alongside `formatPercent` in `utils/scannerFormat.ts`, sharing the same guard discipline (empty/whitespace → raw; non-finite → raw unchanged; never invent a value). The PSRR presentation's `formatMetric` is `formatRatioMultiple`. This is the **only** new logic in the slice; it is named generically (any future dimensionless-ratio metric reuses it) and does not touch the percentage formatters.

## Future implementation files (deferred)

Created: one `previousSessionRelativeRangePresentation` object in `components/scanners/scannerPresentation.ts`, `pages/PreviousSessionRelativeRangeScannerPage.tsx`, targeted tests (presentation metadata; `formatRatioMultiple` incl. `0`/`1`/`>1`/empty/malformed; route mount; a percentage-formatter regression asserting CPR/PSR/PSB unchanged). Modified: `routes/router.tsx` (+1 route), `components/common/Sidebar.tsx` (+1 nav entry), `utils/scannerFormat.ts` (+`formatRatioMultiple` and `RATIO_DISPLAY_DECIMALS`; `formatPercent`/`formatCprWidthPct` untouched). Unchanged: `apiClient.ts`, `scannerClient.ts`, `useScannerSnapshot.ts`, `ScannerTable.tsx`, `ScannerStatusBar.tsx`, `ScannerPanel.tsx`, `types/scanner.ts`, and all backend.

## Consequences

- A fourth scanner page ships as metadata + page + route + nav + **one** new ratio formatter, with zero generic-infrastructure and zero backend change and no ranking branch.
- Narrow CPR / Previous Session Range % / Previous Session Body % stay frozen and byte-identical.
- The dimensionless-ratio display convention (`N.NN×`) is now governed and reusable by any future ratio-metric scanner.
- Implementation deferred to PREVIOUS-SESSION-RELATIVE-RANGE-FRONTEND-IMPL-R1.
