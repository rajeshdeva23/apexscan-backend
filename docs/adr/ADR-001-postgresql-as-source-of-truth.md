# ADR-001 — Use PostgreSQL as the Source of Truth

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-07-31 |
| **Deciders** | Platform / Data Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | `docs/02_DATABASE_DESIGN.md`, `docs/01_SYSTEM_ARCHITECTURE.md` |

---

## Context

ApexScan is a **hybrid storage system** (see `02_DATABASE_DESIGN.md`). It uses
multiple stores — a durable relational store, a hot cache/pub-sub layer
(Redis), and process memory for high-velocity transient data. For this to work,
exactly **one** store must be the authoritative **source of truth**: the record
that survives restarts, enforces relational integrity, and is trusted when the
stores disagree.

The platform must durably persist configuration, users, strategies, strategy
runs, results, and instrument reference data — all of which are relational, must
be transactionally consistent, and must be queryable for history and analysis.
We need to decide which technology holds that durable truth.

## Decision

**Use PostgreSQL as the single source of truth** for all durable, relational
data in ApexScan. Redis and in-memory caches remain strictly non-authoritative:
they accelerate and coordinate, but never hold the only copy of data that must
survive a restart.

## Reasons

- **ACID compliance.** Configuration changes and result snapshots must be atomic
  and durable. A crash must never leave the record of truth half-written.
- **JSONB support.** Flexible, semi-structured fields (e.g. strategy
  configuration, result explanations) can be stored and indexed without
  abandoning relational guarantees or introducing a second database.
- **Excellent indexing.** Rich, well-understood indexing (B-tree, partial,
  composite, GIN for JSONB) supports the read patterns of the scan and dashboard
  paths.
- **Reliable migrations.** A mature migration story (via Alembic) gives us
  reviewed, forward-only, reversible-where-practical schema evolution — critical
  for a system expected to grow to 100+ strategies and multiple brokers.

## Alternatives Considered

| Alternative | Why not chosen |
|-------------|----------------|
| **MySQL** | A capable relational option, but PostgreSQL's stronger JSONB support, richer indexing options, and stricter standards compliance better fit our mixed relational/semi-structured data and analytical query needs. |
| **MongoDB** | A document store trades away the relational integrity and transactional guarantees that our instrument/strategy/result relationships depend on. It would push integrity enforcement into application code — exactly what we want the database to own. |

## Consequences

**Positive**
- One unambiguous system of record; when stores disagree, PostgreSQL wins.
- Relational integrity (foreign keys, constraints) is enforced by the database,
  not by hopeful application code.
- A single, mature operational story for backups, replication, and monitoring.

**Negative / trade-offs**
- PostgreSQL must be operated well (backups, connection pooling, capacity). This
  is an accepted operational cost.
- High-write data (e.g. Strategy Results) needs a deliberate growth plan
  (partitioning/retention) so the source of truth does not degrade under load —
  tracked in `02_DATABASE_DESIGN.md` §8–§9.

**Guardrails established by this decision**
- Redis and in-memory caches are **never** the sole home of data that must
  survive a restart.
- All store access is mediated by the **repository layer**; which store backs a
  given piece of data stays hidden behind that interface.

---

*This ADR records a point-in-time decision. If it is ever revised, mark it
`Superseded by` a new ADR rather than editing the decision in place.*
