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
