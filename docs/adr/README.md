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
