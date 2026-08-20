# Architecture Decision Records (ADRs)

This directory holds ApexScan's **Architecture Decision Records** — short,
immutable documents that capture a significant architectural decision, the
context that forced it, the alternatives weighed, and its consequences.

## Conventions

- **One decision per file**, named `ADR-NNN-short-slug.md`.
- **Numbers are sequential and never reused.**
- An ADR is **immutable once Accepted.** To change a decision, write a new ADR
  and mark the old one `Superseded by ADR-NNN` (do not rewrite history).
- **Status** is one of: `Proposed`, `Accepted`, `Superseded`, `Deprecated`.

## Log

| ADR | Title | Status | Date |
|-----|-------|--------|------|
| [ADR-001](ADR-001-postgresql-as-source-of-truth.md) | Use PostgreSQL as the Source of Truth | Accepted | 2026-07-31 |
| [ADR-002](ADR-002-two-repository-ownership.md) | Separate ApexScan into Backend and Frontend Repositories | Accepted | 2026-08-03 |
| [ADR-003](ADR-003-broker-adapter-pattern.md) | Adopt the Broker Adapter Pattern | Accepted | 2026-08-04 |
| [ADR-004](ADR-004-nse-cash-equity-live-scanner-domain.md) | Use NSE Cash Equity as the V1 Live Scanner Domain | Accepted | 2026-08-06 |
| [ADR-005](ADR-005-canonical-session-cumulative-volume.md) | Canonical Session Cumulative Volume for Live Candle Aggregation | Accepted | 2026-08-06 |
| [ADR-006](ADR-006-candle-completeness-feed-continuity-volume-reconciliation.md) | Exact Candle Completeness, Feed Continuity, and Volume Reconciliation | Accepted | 2026-08-07 |
| [ADR-007](ADR-007-dynamic-strategy-lifecycle-requirement-management.md) | Dynamic Strategy Lifecycle & Requirement Management | Accepted | 2026-08-09 |
| [ADR-008](ADR-008-authoritative-current-session-statistics.md) | Authoritative Provider-Supplied Current-Session Statistics | Accepted | 2026-08-10 |
| [ADR-009](ADR-009-rest-backed-authoritative-session-statistics.md) | REST-Backed Authoritative Current-Session Statistics | Accepted | 2026-08-11 |
| [ADR-010](ADR-010-live-market-runtime-composition-and-managed-ingestion-lifecycle.md) | Live-Market Runtime Composition & Managed Ingestion Lifecycle | Accepted | 2026-08-12 |
| [ADR-011](ADR-011-historical-calendar-authority-window.md) | Historical Trading-Calendar Authority Window | Accepted | 2026-08-13 |
| [ADR-012](ADR-012-cross-instrument-strategy-scanner.md) | Cross-Instrument Strategy Scanner | Accepted | 2026-08-16 |
| [ADR-013](ADR-013-production-strategy-registration.md) | Production Strategy Registration & Enablement (Strategy Catalog) | Accepted | 2026-08-16 |

## Subordinate operational & evidence artifacts

These are not numbered ADRs; each is explicitly subordinate to the ADR it supports and does
not alter that ADR's Accepted decisions.

| Artifact | Subordinate to | Purpose |
|----------|----------------|---------|
| [ADR-007 Narrow CPR strategy specification](ADR-007-narrow-cpr-strategy-specification.md) | ADR-007 | V1 Narrow CPR contract: pivot-normalized CPR width from the previous completed session; non-directional ranking feature; no-look-ahead; historical-relative narrowness + cross-instrument scanner surface deferred; implementation deferred |
| [ADR-007 Previous Session Body % strategy specification](ADR-007-previous-session-body-pct-strategy-specification.md) | ADR-007 | V1 spec (PSB1-27) for a completed-session-only, non-directional scanner: rank by previous-session `\|close−open\|/open×100` DESCENDING (largest absolute body first); `HistoricalRequirement(session,1)` + `FactNeed.PREVIOUS_SESSION`; ON_HISTORICAL_READY / ONE_SHOT_PER_SESSION; score=None; reuses generic scanner/REST/frontend (`formatPercent`); no current-session authority; zero new tasks; implementation deferred |
| [ADR-007 Previous Session Range % strategy specification](ADR-007-previous-session-range-pct-strategy-specification.md) | ADR-007 | V1 spec (PSR1-24) for a completed-session-only, non-directional scanner: rank by previous-session `(high−low)/open×100` DESCENDING (expansion); `HistoricalRequirement(session,1)` + `FactNeed.PREVIOUS_SESSION`; ON_HISTORICAL_READY / ONE_SHOT_PER_SESSION; score=None; reuses generic scanner/REST/frontend; no current-session authority; zero new tasks; implementation deferred |
| [ADR-007 Previous Session Relative Range strategy specification](ADR-007-previous-session-relative-range-strategy-specification.md) | ADR-007 | V1 spec (PSRR1-30) for the first multi-session scanner: rank by `range_pct(D-1) / median(range_pct over D-2…D-21)` ASCENDING (most compressed vs own 20-session baseline); fixed baseline=20, `HistoricalRequirement(session,21)`; exact even-N Decimal median; degenerate-baseline & missing-history → SKIPPED; basis-safe (per-session dimensionless); reuses generic scanner/REST; no current-session authority; zero new tasks; implementation deferred |
| [ADR-007 multi-session historical lookback capability](ADR-007-multi-session-historical-lookback-capability.md) | ADR-007 | MSH1-16 capability governance for `HistoricalRequirement(session, N>1)`: subsystem already populates `HistoricalContext.series` with N completed authoritative sessions (oldest→newest, calendar-aware, current-day excluded, max-lookback union, no new task); insufficient-coverage global vs local-gap PARTIAL; per-session normalized-% ops basis-safe, raw cross-session price ops BLOCKED on ungoverned price basis; OUTCOME A → E2E validation deferred |
| [ADR-007 partial-universe historical readiness](ADR-007-partial-universe-historical-readiness.md) | ADR-007 | Refines the D3 START readiness boundary: strategy RUNNING at infrastructure level + per-instrument evaluation readiness; one un-warmable instrument yields honest scanner PARTIAL (not whole-strategy ERROR); global vs local failure taxonomy; no data weakening; implementation deferred |
| [ADR-008 provider-verification record](ADR-008-provider-verification-record.md) | ADR-008 | WebSocket day-OHLC verification evidence (BLOCKED) |
| [ADR-008 provider-evidence closure plan](ADR-008-provider-evidence-closure-plan.md) | ADR-008 | Controlled-verification design |
| [ADR-009 provider-verification record](ADR-009-provider-verification-record.md) | ADR-009 | REST OHLC verification evidence (E6/E6B BLOCKED) |
| [ADR-009 refresh-phase execution addendum](ADR-009-refresh-phase-execution-addendum.md) | ADR-009 | When the refresh driver may poll, by market phase |
| [ADR-009 current-session OHLC authority enablement path](ADR-009-current-session-ohlc-authority-enable-path.md) | ADR-009 / ADR-008 | CSOA1-24 evidence-backed path to enable one verified current-session OHLC source (unblocks Open=High/Low); both sources remain FAILED/False, L2 NOT_AVAILABLE, L3 NOT_AUTHORIZED, intraday high/low oracle UNRESOLVED; no bit changed; implementation deferred |
| [ADR-011 calendar-exception-model addendum](ADR-011-calendar-exception-model-addendum.md) | ADR-011 | Open-session overrides + H3 partial intraday authority for the historical calendar |
| [ADR-011 multi-interval-session addendum](ADR-011-multi-interval-session-addendum.md) | ADR-011 | Multiple disjoint live-market intervals per exceptional trading date (corrects the single-interval override) |
| [ADR-011 calendar-monitor governance](ADR-011-calendar-monitor-governance.md) | ADR-011 | Dhan public holiday page as secondary monitoring evidence only; implementation deferred until the authoritative dataset exists |
| [ADR-011 NSE calendar evidence record](ADR-011-nse-calendar-evidence-record.md) | ADR-011 | Primary NSE 2026 Capital-Market holiday evidence (CMTR/71775 verbatim + recorded amendment/special-session facts); date-level authority PASS |
| [ADR-011 live-calendar-source governance](ADR-011-live-calendar-source-governance.md) | ADR-011 | Outcome B — unify date-level live trading-day classification onto the dataset (within coverage, fail-closed outside); intraday hours stay separate; implementation deferred |
| [ADR-011 live out-of-coverage addendum](ADR-011-live-calendar-out-of-coverage-addendum.md) | ADR-011 | Out-of-coverage live classification contract: add `MarketState.CALENDAR_UNAVAILABLE` (classifier owns CalendarCoverage; fail-closed at classify time); implementation deferred |
| [ADR-010 offline validation harness](ADR-010-offline-validation-harness.md) | ADR-010 | Dev/validation-only offline app composition (`app.offline`): real scanner REST + runtime pipeline over in-memory DB/Redis + a synthetic 208-instrument fixture provider (205 ranked / 3 unavailable); no PostgreSQL/Redis/Dhan/network; production `app.main` untouched |
| [ADR-011 calendar-dataset failure policy](ADR-011-calendar-dataset-failure-policy.md) | ADR-011 | Enabled-runtime authoritative-dataset load failure → fail-fast composition/startup (`AuthoritativeCalendarUnavailableError`); no legacy/secondary calendar fallback; implementation deferred |
| [ADR-012 scanner REST API addendum](ADR-012-scanner-rest-api-addendum.md) | ADR-012 | Read-only generic REST transport `GET /api/v1/scanners/{strategy_id}` for scanner snapshots (Decimal-as-string, rank order preserved, PARTIAL/COMPLETE explicit, 503/404 semantics); WebSocket/persistence deferred; implementation deferred |
| [ADR-012 Previous Session Body % frontend integration](ADR-012-previous-session-body-pct-frontend-integration.md) | ADR-012 | FE-PSB1-24: thin frontend for `previous_session_body_pct` (presentation object + page + `/scanners/previous-session-body-pct` route + nav) reusing the generic scanner UI and `formatPercent` unchanged; backend rank DESCENDING authoritative, no client calc/re-rank; absolute-body ⇒ non-directional; Narrow CPR + PSR frozen; no backend/PG/WS/authority change; implementation deferred |
| [ADR-012 Previous Session Range % frontend integration](ADR-012-previous-session-range-pct-frontend-integration.md) | ADR-012 | FE-PSR1-24: thin frontend for `previous_session_range_pct` (presentation metadata + page + `/scanners/previous-session-range-pct` route + nav) reusing the generic scanner UI unchanged; backend rank DESCENDING authoritative, no client calc/re-rank; extract generic `formatPercent` (CPR delegates); Narrow CPR frozen; no backend/PG/WS/authority change; implementation deferred |
| [ADR-012 Narrow CPR frontend integration](ADR-012-narrow-cpr-frontend-integration.md) | ADR-012 | React dashboard integration of the Narrow CPR scanner over the existing REST endpoint (conforms to `04_FRONTEND_ARCHITECTURE.md`): REST polling (WebSocket deferred), generic scanner client/hook/table, backend-authoritative rank order, honest PARTIAL/COMPLETE + derived missing-count, non-directional language; no backend change; implementation deferred |
