# 11 · Coding Guidelines

> **Official Engineering Standards Manual for ApexScan**
> This document defines **how** software is written for ApexScan. Every developer, every AI coding
> assistant (Codex, ChatGPT, Claude, and any future tool), and every future contributor **must** follow
> it. It is a **standards document only**: no code, no Python, no React, no SQL, no YAML, no Docker. It
> defines *how* software is written — never *what* the software does (that is the job of documents
> `00`–`10`).

---

## Document Banner

| Field | Value |
|-------|-------|
| Document | `11_CODING_GUIDELINES.md` |
| Title | Engineering Standards Manual |
| Status | **Authoritative & Binding** — applies to all contributors, human and AI |
| Scope | How code is written, reviewed, tested, and merged |
| Owner | Engineering Excellence |
| Governs | All source in the repository |
| Related | `01_SYSTEM_ARCHITECTURE.md`, `03_BACKEND_ARCHITECTURE.md`, `04_FRONTEND_ARCHITECTURE.md`, `05`–`10`, `docs/adr/` |

> **How to read this document.**
> - Documents `00`–`10` define **WHAT** ApexScan is and does (architecture, contracts, deployment).
> - This document (`11`) defines **HOW** it is built (standards, discipline, review).
> - The ADRs (`docs/adr/`) record **WHY** specific decisions were made and are **binding**.
>
> When this document and an architecture document appear to conflict, the architecture document wins on
> *what*, this document wins on *how*, and an ADR wins on *why*. Unresolved conflicts are escalated, not
> guessed.

---

## Mini Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Engineering Principles](#2-engineering-principles)
3. [Repository Standards](#3-repository-standards)
4. [Python Standards](#4-python-standards)
5. [FastAPI Standards](#5-fastapi-standards)
6. [React Standards](#6-react-standards)
7. [TypeScript Standards](#7-typescript-standards)
8. [Database Coding Standards](#8-database-coding-standards)
9. [Async Programming Standards](#9-async-programming-standards)
10. [Error Handling Standards](#10-error-handling-standards)
11. [Logging Standards](#11-logging-standards)
12. [Testing Standards](#12-testing-standards)
13. [Git Standards](#13-git-standards)
14. [Documentation Standards](#14-documentation-standards)
15. [Security Standards](#15-security-standards)
16. [Performance Standards](#16-performance-standards)
17. [AI Development Guidelines](#17-ai-development-guidelines)
18. [Code Review Standards](#18-code-review-standards)
19. [Non-Negotiable Engineering Rules](#19-non-negotiable-engineering-rules)
20. [Engineering Checklist](#20-engineering-checklist)
21. [Summary](#21-summary)

---

## 1. Executive Summary

ApexScan is intended to outlive any individual contributor and any single implementation of its
components. That longevity is only possible if the code is written to a **shared, explicit standard**
rather than to each author's personal taste. This document is that standard.

### 1.1 Engineering Philosophy

- **Code is read far more often than it is written.** Optimize for the reader — the next engineer, the
  reviewer, and the AI assistant six months from now — not for the convenience of the author today.
- **The architecture is the constraint, not a suggestion.** Standards exist to keep every contribution
  inside the boundaries defined by documents `00`–`10`. Clever code that violates a boundary is a defect.
- **Consistency beats individual brilliance.** A codebase where every file looks like every other file
  is faster to understand, safer to change, and easier to review than a collection of locally-optimal but
  globally-inconsistent parts.

### 1.2 Maintainability

Maintainability is the primary quality attribute. A change should be **local, obvious, and safe**:
localized because concerns are separated (§2), obvious because names and structure reveal intent, and
safe because tests and types catch regressions. Every standard here serves maintainability first.

### 1.3 Readability

Readable code needs no comment to explain *what* it does; its structure and naming say it. Comments
explain *why*, never *what* (§14). Density and cleverness that impress the author but slow the reader are
discouraged: **clarity over cleverness, always.**

### 1.4 Consistency

One naming scheme, one error model, one logging shape, one project structure — applied everywhere.
Consistency is enforced by automated tooling (linters, formatters, type checkers) so it is not a matter
of reviewer memory or goodwill (§18, §19).

### 1.5 Long-Term Ownership

Code is written as if the author will still own it in three years and as if a stranger will own it
tomorrow — because both are true. This means no orphaned modules, no "temporary" hacks that become
permanent, and no undocumented cleverness. Dead code is removed, not deprecated (§3, §19).

### 1.6 Code-Quality Culture

Quality is a **team norm**, upheld by review (§18), automation (§1.4), and a zero-warnings baseline
(§4, §19). It is everyone's job — including every AI assistant's (§17). A warning ignored today is an
incident tomorrow.

> **Architecture Callout — standards protect the architecture.** Every principle, rule, and checklist
> item in this document ultimately exists to keep the layered, event-driven, boundary-respecting
> architecture of ApexScan intact as many hands (and many models) contribute to it.

---

## 2. Engineering Principles

These principles are the *reasoning tools* behind the concrete rules later in the document. Each is
stated with its purpose, its benefits, when to apply it, and the common mistakes it prevents.

### 2.1 SOLID

| Aspect | Statement |
|--------|-----------|
| Purpose | Keep object/module design flexible and change-tolerant. |
| Benefits | Localized change, testability, replaceable parts (brokers, strategies). |
| When to apply | Any time behaviour is likely to vary or grow (which is most of ApexScan). |
| Common mistakes | Treating SOLID as ceremony; adding abstraction with no second implementation in sight (violates YAGNI). |

SOLID's five facets map directly onto ApexScan's boundaries: the broker adapter (§05), the strategy
plug-in contract (§07), and the repository pattern (§08) are all SOLID in practice.

### 2.2 DRY (Don't Repeat Yourself)

- **Purpose:** one authoritative place for each piece of knowledge.
- **Benefits:** a rule changes in exactly one location; no divergent copies drift apart.
- **When to apply:** when the *same knowledge* appears twice — not merely when code looks similar.
- **Common mistakes:** over-DRYing coincidental similarity into a brittle shared abstraction. Duplication
  is cheaper than the wrong abstraction; extract only on the **third** repetition (§3).

### 2.3 KISS (Keep It Simple)

- **Purpose:** prefer the simplest design that meets the requirement.
- **Benefits:** fewer moving parts, fewer bugs, faster onboarding.
- **When to apply:** always; complexity must be *earned* by a real need.
- **Common mistakes:** building for imagined future scale (see YAGNI); mistaking simple for simplistic.

### 2.4 YAGNI (You Aren't Gonna Need It)

- **Purpose:** don't build features, flags, or abstractions until a real need exists.
- **Benefits:** less code, less attack surface, less maintenance.
- **When to apply:** whenever tempted to add "just in case" configurability.
- **Common mistakes:** speculative generality; premature plug-in points; phantom features documented but
  unbuilt.

### 2.5 Single Responsibility

- **Purpose:** each module/class/function does one thing and has one reason to change.
- **Benefits:** small, testable, composable units; clean boundaries.
- **When to apply:** everywhere; it is the backbone of the layered architecture.
- **Common mistakes:** "and" in a function's description; a service that both computes and persists and
  formats.

### 2.6 Open/Closed Principle

- **Purpose:** open for extension, closed for modification.
- **Benefits:** new strategies/brokers are *added*, not grafted into existing code (mirrors `07` §19).
- **When to apply:** at every documented extension seam (strategies, adapters, API façades).
- **Common mistakes:** editing the engine to add a strategy — the canonical violation this codebase
  forbids.

### 2.7 Composition over Inheritance

- **Purpose:** build behaviour by combining small parts rather than deep class hierarchies.
- **Benefits:** flexibility, testability, no fragile base classes.
- **When to apply:** default choice for sharing behaviour.
- **Common mistakes:** inheritance for code reuse rather than for genuine "is-a" relationships.

### 2.8 Fail Fast

- **Purpose:** detect and surface invalid state at the earliest possible point.
- **Benefits:** bugs are caught near their cause, not far downstream; no half-initialized services.
- **When to apply:** input validation, configuration loading (§4, `10` §6.4), invariant checks.
- **Common mistakes:** swallowing errors to "keep going"; defaulting past a missing required value.

### 2.9 Deterministic Systems

- **Purpose:** given the same input, produce the same output (critical for the engines — `06`/`07`).
- **Benefits:** reproducibility, replay testing (§12), trustworthy scanner results.
- **When to apply:** all fact/result computation; anywhere correctness is verifiable.
- **Common mistakes:** hidden dependence on wall-clock time, ordering, or randomness (inject them
  instead).

### 2.10 Defensive Programming

- **Purpose:** protect a module from bad inputs *at its boundary*, then trust internally.
- **Benefits:** robust boundaries, clean interiors.
- **When to apply:** at public entry points (API, adapters, service boundaries).
- **Common mistakes:** defensiveness *everywhere* (noise, redundant checks) instead of *at the seam*.

### 2.11 Separation of Concerns

- **Purpose:** keep distinct responsibilities in distinct places (transport, business, persistence,
  presentation).
- **Benefits:** the whole layered architecture; independent evolution of each layer.
- **When to apply:** always; it is the meta-principle the codebase is organized around.
- **Common mistakes:** business logic in the API layer, formatting in the service layer, SQL in a handler.

> **Architecture Callout — principles resolve conflicts.** When two rules seem to collide, reason from
> these principles. Example: "DRY says extract; KISS says don't" → resolve with the rule of three (§2.2)
> and Separation of Concerns. Principles are the tie-breakers.

---

## 3. Repository Standards

### 3.1 Folder & File Organization

- The repository is organized by the **layers and components** defined in `03`/`04` (adapters, market
  engine, strategies, services, repositories, API, frontend layers). Structure mirrors architecture.
- A file contains **one coherent unit** (one primary class/component/concern). Files do not become
  grab-bags.
- Naming is descriptive and consistent within each language's convention (§4, §6, §7).

### 3.2 Maximum File-Size Philosophy

- Files and functions are kept **small enough to hold in one's head**. Hard limits (from the global
  standards) apply: functions ≤100 lines and cyclomatic complexity ≤8; a file that outgrows a single
  responsibility is **split**, not extended.
- Size is a *smell*, not a law of its own: a large file is a prompt to check whether Single
  Responsibility (§2.5) is being violated.

### 3.3 Module Ownership

Each module has a **clear owner concern** and a documented reason to exist. There are no ambiguous
"utils" dumping grounds; a utility earns its place only after the rule of three (§2.2) and lives with a
clear name.

### 3.4 Dependency Direction

> ⚠️ **The Dependency Rule is absolute.** Dependencies point **inward**: outer layers depend on inner
> layers, never the reverse (see `01`/`03`). The Market Engine never imports strategies; strategies never
> import brokers; repositories never import services; the API never contains business logic.

### 3.5 Import Hierarchy

- **Absolute imports only** — no relative parent-traversal imports. This keeps moves safe and the
  dependency graph legible.
- Imports are grouped and ordered consistently (standard library, third-party, first-party), enforced by
  tooling.

### 3.6 Circular-Dependency Prevention

- Circular dependencies are **forbidden** and enforced by tooling in CI. A cycle is a design error,
  resolved by introducing an interface or moving the shared concept inward — never by a lazy import hack.

### 3.7 Package Organization & Future Expansion

- Packages are organized so that the platform can grow to **100+ strategies and multiple brokers** without
  restructuring (the founding requirement). New strategies and adapters slot into their existing seams
  (§2.6).
- Planned-but-unbuilt packages are marked as **planned/target** in the architecture docs, not created
  empty and left to rot.

---

## 4. Python Standards

*(Standards only — no code, no examples.)*

### 4.1 Style & PEP 8

- **PEP 8 is the baseline**, enforced by an automated formatter and linter; formatting is never a review
  topic because tooling settles it. Line length follows the project limit (100 chars).
- **Zero-warnings policy:** every linter/type-checker warning is fixed or explicitly justified with an
  inline suppression and reason. A clean run is the baseline, not the goal.

### 4.2 Naming Conventions

- Modules and packages: short, lowercase, descriptive. Classes: PascalCase. Functions/variables:
  snake_case. Constants: UPPER_SNAKE_CASE. Names reveal **intent and unit** (e.g., a duration name states
  its unit).
- No abbreviations that a newcomer wouldn't recognize; no single-letter names outside tiny, conventional
  scopes.

### 4.3 Typing & Type Hints

- **Type hints are mandatory** on all public functions, method signatures, and module-level values. The
  codebase is fully type-checked in strict mode; untyped public surfaces are not accepted.
- Types express **domain meaning**, not just primitives, where a distinct concept exists.

### 4.4 Dataclasses, Enums, Constants

- **Immutable data carriers** (e.g., frozen dataclasses / value objects) are preferred for facts and
  results — consistent with the immutable `MarketContext`/`StrategyResult` model (`06`/`07`).
- **Enums** replace magic strings for closed sets of values.
- **Constants** replace magic numbers and repeated literals; there are no unexplained numeric literals in
  logic (§4.9).

### 4.5 Module Organization

- One module = one concern (§3.1). Public surface is explicit; internal helpers are clearly private.
- Module-level side effects at import time are forbidden (they break testability and determinism).

### 4.6 Async vs Sync Functions

- The backend is **async-first** (§9). I/O-bound work (DB, Redis, broker, network) is async; blocking
  calls never run on the event loop.
- CPU-bound or genuinely synchronous work is isolated and, where needed, offloaded so it cannot stall the
  loop (§9.6).

### 4.7 Exception Philosophy

- Exceptions signal **exceptional** conditions, not ordinary control flow.
- Errors **fail fast** with clear, actionable messages including context (what operation, what input,
  suggested fix). Exceptions are **never swallowed silently** (§10, §19).

### 4.8 Logging & Documentation

- Logging is **structured** (§11); no ad-hoc print statements in committed code.
- **Google-style docstrings** on all non-trivial public APIs, stating purpose, parameters, returns, and
  raised errors — the *why* and the *contract*, not a restatement of the code.

### 4.9 Imports, Magic Numbers, Configuration Access

- Absolute imports only (§3.5); no wildcard imports.
- **No magic numbers/strings** in logic — named constants or enums instead (§4.4).
- **Configuration is accessed through the settings abstraction**, never by reading environment variables
  ad hoc throughout the code (single source, validated at startup — `10` §6).

> ⚠️ **No globals, no import-time side effects, no swallowed exceptions.** These three are the most
> common ways Python code quietly becomes untestable and unpredictable. All are forbidden (§19).

---

## 5. FastAPI Standards

*(Standards only — no code, no examples. Implements the contract in `08`; internals per `03`.)*

### 5.1 API Organization

- Endpoints are grouped by **resource/category** (`08` §5) and mounted under an explicit **version**
  (`08` §8). Routing is thin; it delegates immediately to the service layer.

### 5.2 Dependency Injection

- Cross-cutting resources (settings, database session, cache, future auth identity) are supplied via
  **dependency injection**, never constructed ad hoc inside handlers. This keeps handlers testable and
  the wiring explicit.

### 5.3 Validation

- **All input is validated at the boundary** using schema models before any business logic runs (fail
  fast, `08` §7.1). Validation rejects unknown/malformed input (fails closed).

### 5.4 Service Layer

- Handlers contain **no business logic**. They validate, delegate to a service, and shape the response.
  Business decisions live in the service layer (`03`, `08` §2).

### 5.5 Repository Layer

- Services reach persistence **only through repositories** (§8). No handler or service issues raw queries;
  no repository contains business logic.

### 5.6 Response Consistency & Error Handling

- Responses conform to the **uniform contract shape** and the **single error model** (`08` §3.9, §7).
  Every failure is expressed through that model; no endpoint invents its own error format.

### 5.7 Versioning

- The API is **explicitly versioned** and evolves **additively** within a major version (`08` §8). No
  breaking change ships inside an existing version.

> **Architecture Callout — the handler is a doorway.** A FastAPI handler validates, authorizes (future),
> delegates, and shapes. If a handler computes a business result or touches the database directly, the
> layering has been breached.

---

## 6. React Standards

*(Standards only — no code, no examples. Aligns with `04`.)*

### 6.1 Component Organization

- Components live in the structure defined by `04` (components, pages, layouts, hooks, services, store,
  types, utils). One component per file; the filename matches the component.

### 6.2 Functional Components & Hooks

- **Functional components only**; class components are not used.
- Logic is composed through **hooks**; custom hooks encapsulate reusable stateful logic with clear,
  descriptive names. The rules of hooks are always respected.

### 6.3 State Ownership

- **Client/UI state** lives in the client store (Zustand); **server state** lives in the data-fetching
  layer (TanStack Query). The two are never conflated — server data is not hand-copied into client state
  (`04`).
- State is owned at the **lowest component that needs it**; lifting happens only when genuinely shared.

### 6.4 Props & Composition

- Props are **explicitly typed** (§7) and minimal; components communicate through props and composition,
  not hidden global reach.
- **Composition over configuration:** prefer composing small components over giant components with many
  boolean flags.

### 6.5 Layouts, Pages, Shared Components

- **Layouts** own structural chrome; **pages** own route-level composition; **shared components** are
  generic and presentation-focused. Each tier has a clear role (`04`).

### 6.6 Container vs Presentational

- Separate **data/behaviour** (container concerns: fetching, state) from **presentation** (pure rendering
  from props). Presentational components are easy to test and reuse; containers wire them to data.

> **Architecture Callout — the frontend renders, it does not decide.** As in `09`, the UI reflects
> truth produced upstream. Components must not re-rank, re-compute, or reinterpret server-provided
> results; they present them.

---

## 7. TypeScript Standards

### 7.1 Strict Mode

- **Strict mode is mandatory.** `any` is not permitted except at an explicitly justified, isolated
  boundary with a comment; implicit `any` is a build failure. Nullability is modeled honestly.

### 7.2 Types vs Interfaces

- Use a **consistent convention** (documented in the repo) for when to use `type` vs `interface` —
  typically interfaces for extensible object shapes/public contracts, type aliases for unions and
  composed types. Consistency matters more than the specific choice.

### 7.3 Naming

- Types/interfaces: PascalCase. Variables/functions: camelCase. Names describe **domain meaning**, mirror
  backend concepts where they cross the wire, and avoid Hungarian prefixes.

### 7.4 Readonly Philosophy

- Prefer **`readonly`/immutable** shapes for data that should not be mutated after creation (server data,
  props). Immutability is the default; mutation is the deliberate exception.

### 7.5 Enums, Generics, Utility Types

- Use closed **enums/union literals** for fixed value sets. Use **generics** to avoid duplication while
  preserving type safety. Use built-in **utility types** rather than hand-rolling equivalents.

### 7.6 Type Ownership

- Types that mirror the API contract (`08`) have a **single source** in the types layer and are not
  redefined per component. Shared domain types are owned centrally; component-local types stay local.

---

## 8. Database Coding Standards

*(Standards only — no SQL. Implements `02`; accessed per `03`.)*

### 8.1 Repositories

- **All persistence goes through repositories.** A repository is the only place that knows how data is
  stored; it exposes intention-revealing operations and contains **no business logic** (§19).

### 8.2 Transactions

- Operations that must be atomic are wrapped in a **transaction** owned at the service boundary. Partial
  writes are never left committed; failures roll back cleanly.

### 8.3 Read vs Write

- Read and write paths are **distinguished**; reads avoid unnecessary locking, writes are explicit about
  their transactional scope. Read replicas (future, `10` §13.5) are transparent to callers via the
  repository.

### 8.4 Connection Handling

- Connections/sessions are obtained via **dependency injection** and **always released** (no leaks), even
  on error. Pooling is respected; no per-call ad-hoc connections.

### 8.5 Migration Philosophy

- Schema changes ship as **versioned, reviewed migrations** (Alembic). Migrations are **forward-only in
  spirit**, reversible where feasible, and never hand-applied to a running database (`10` §17).
- The database schema is the **source of truth for structure**; code conforms to migrations, not the
  reverse.

### 8.6 Naming

- Database object names follow a **single documented convention** (consistent casing and pluralization),
  applied uniformly (`02`).

> ⚠️ **PostgreSQL is the source of truth (ADR-001); Redis is never authoritative.** Code must never treat
> a cache value as canonical, and must tolerate its absence by falling through to the source (`10` §15).

---

## 9. Async Programming Standards

### 9.1 Async-First Philosophy

- The backend is **async-first**: all I/O-bound paths (DB, cache, broker, HTTP, WebSocket) are
  asynchronous so the event loop stays responsive under concurrent load.

### 9.2 Await Discipline

- Every awaitable is **awaited or deliberately scheduled** — never fired and forgotten by accident.
  "Orphaned" coroutines and un-awaited tasks are bugs.

### 9.3 Cancellation

- Code is **cancellation-aware**: long-running async work handles cancellation cleanly, releasing
  resources. Cancellation is a normal signal, not an error to be swallowed.

### 9.4 Timeouts

- External calls (broker, network, DB) carry **explicit timeouts**. No async operation waits forever; a
  timeout is a defined, handled outcome (`05`, `10` §8).

### 9.5 Concurrency

- Concurrency primitives are used **intentionally and bounded**; unbounded fan-out is forbidden (mirrors
  the bounded fan-out discipline in `09` §11). Shared mutable state across tasks is avoided or explicitly
  synchronized.

### 9.6 Blocking Code & Background Tasks

- **Blocking calls never run on the event loop.** CPU-bound or blocking work is offloaded appropriately.
- Background/long-running work uses the platform's defined mechanism (planned `workers/` — `03`), is
  observable, and shuts down gracefully (`10` §8.3).

### 9.7 Event-Driven Programming

- Producers publish typed events and forget them; consumers subscribe (mirrors `01`/`09`). Components do
  not reach across the event boundary to call each other directly when an event is the right seam.

> **Architecture Callout — the event loop is a shared resource.** One blocking call stalls *everything*.
> Async discipline is not a style preference here; it is what keeps a real-time scanner real-time.

---

## 10. Error Handling Standards

### 10.1 Expected vs Unexpected Failures

| Kind | Handling |
|------|----------|
| **Expected failure** (invalid input, not-found, rule violation) | Modeled explicitly, surfaced through the uniform error model (`08` §7); not a crash, not a stack trace to the user. |
| **Unexpected failure** (bug, dependency down) | Fail fast, log with context and correlation id, return a safe generic error; never leak internals. |

### 10.2 Validation

- Validate **at the boundary**, before business logic (§5.3, §2.10). Validation errors are precise about
  *what* and *where*.

### 10.3 Exceptions & Retries

- Exceptions carry **actionable context**. Retries apply **only** to transient, idempotent operations,
  with **backoff + jitter** and a bounded ceiling (mirrors `08` §7.5, `09` §4.3). Non-idempotent or
  business failures are **not** retried blindly.

### 10.4 Logging & Recovery

- Every handled failure is **logged once, with context** (§11) — not logged repeatedly at every layer as
  it bubbles. Recovery paths are explicit and tested (§12); the system degrades honestly (§16, `09`/`10`).

### 10.5 User-Facing Errors

- User-facing errors are **safe, clear, and non-leaking**: no stack traces, queries, secrets, or internal
  identifiers ever reach a client (`08` §7.5). They carry a correlation id so support can trace the real
  cause server-side.

> ⚠️ **Never swallow an exception.** Catching an error only to ignore it hides bugs and corrupts state
> silently. Catch to *handle* (recover, translate, or re-raise with context) — never to *silence* (§19).

---

## 11. Logging Standards

### 11.1 Structured Logging

- Logs are **structured** (machine-parseable key/values), not free-form strings, so they can be searched,
  correlated, and centralized (`10` §9).

### 11.2 Log Levels

- Levels are used **consistently and meaningfully** (debug/info/warning/error/critical). Noise is kept out
  of higher levels; an `error` means something actually needs attention.

### 11.3 Correlation IDs

- Every log line participating in a request/stream carries a **correlation id** so a single interaction can
  be traced across API → service → repository → event/stream (`08` §13.4, `09` §13).

### 11.4 Sensitive Information

> ⚠️ **Never log secrets or sensitive data.** Credentials, tokens, keys, and PII are **never** written to
> logs (`10` §9.3, §12). Log scrubbing is mandatory, not best-effort.

### 11.5 Domain Log Categories

| Category | Purpose |
|----------|---------|
| **Audit logs** | Who/what changed configuration or state (foundational for future trading — `08` §5). |
| **Performance logs** | Latency and timing signals for budgets (`08`/`09` §11). |
| **Broker logs** | Data Provider connectivity, feed health, reconnects (`05`, `09` §10.4) — without leaking credentials. |
| **Strategy logs** | Strategy execution, faults, and health (`07` §22) — facts about execution, never a leak of proprietary logic beyond what's intended. |

---

## 12. Testing Standards

### 12.1 Test Types

| Type | What it verifies |
|------|------------------|
| **Unit** | A single unit's behaviour in isolation (mock only boundaries — slow, non-deterministic, external). |
| **Integration** | Real collaboration across layers (service ↔ repository ↔ database). |
| **Contract** | The API/event contract (`08`/`09`) does not break for consumers. |
| **Regression** | A previously fixed bug stays fixed. |
| **Performance** | Latency/throughput budgets hold (`08`/`09`/`16`). |
| **Replay** | Deterministic engines reproduce identical results from recorded inputs (`06`/`07` — the determinism guarantee). |

### 12.2 Testing Philosophy

- **Test behaviour, not implementation.** A refactor that preserves behaviour must not break tests; if it
  does, the tests were testing the wrong thing.
- **Test edges and errors, not just the happy path.** Empty inputs, boundaries, malformed data, failures,
  and cancellation each get a test.
- **Mock boundaries, not logic.** Only mock the slow, non-deterministic, or external; never mock the code
  under test.
- **Verify tests catch failures.** Break the code, confirm the test fails, then fix — mutation/property
  testing where it adds confidence.

### 12.3 Naming

- Test names state the **scenario and expected outcome** so a failure reads like a sentence describing what
  broke.

### 12.4 Coverage Philosophy

- Coverage is a **signal, not a target.** High coverage of meaningful behaviour matters; chasing a
  percentage with trivial tests does not. Critical paths (engines, contracts, error handling) are covered
  thoroughly; uncovered critical paths block merge (§18).

> **Architecture Callout — determinism is testable, and therefore tested.** Because the engines are
> deterministic (§2.9), replay testing can assert exact reproduction. This is a first-class test type, not
> an afterthought — it is how the scanner's trustworthiness is proven.

---

## 13. Git Standards

### 13.1 Branch Naming

- Branches follow a **documented, prefix-based convention** (e.g., feature/…, fix/…, hotfix/…, chore/…)
  with a short, descriptive slug. No work happens directly on the default branch (§19).

### 13.2 Commit Messages

- **Imperative mood**, ≤72-char subject, one **logical change** per commit. The body explains *why* when
  it isn't obvious. Commits are atomic and reviewable.

### 13.3 Pull Requests

- Every change reaches the default branch via a **reviewed pull request**. The PR describes **what the
  code does now** — not discarded approaches or prior iterations — in plain, factual language (no
  "critical", "robust", "comprehensive" inflation).

### 13.4 Merge Policy

- Merges require **green CI** (lint, type-check, tests) and **approving review** (§18). No self-merge of
  unreviewed changes; no merging with failing checks or unresolved review threads.

### 13.5 Release Tags & Versioning

- Releases are **tagged** with a **semantic version**. The API's URI version (`08` §8) and the release
  version are kept coherent. Breaking changes bump the appropriate component.

### 13.6 Hotfixes

- Hotfixes follow an expedited but **still reviewed and tested** path, are tagged, and are **merged back**
  so the fix is never lost in a subsequent release.

> ⚠️ **Never rewrite shared history, never push to the default branch directly, never commit secrets.**
> These three git rules are absolute (§15, §19).

---

## 14. Documentation Standards

### 14.1 Markdown & Architecture Documents

- All project documentation is **professional Markdown**, consistent with the `docs/00`–`10` house style
  (banners, tables, callouts, Mermaid where diagrams help). Architecture documents define **WHAT**; this
  manual defines **HOW**.

### 14.2 ADRs & RFCs

- **ADRs** record accepted decisions and their rationale in `docs/adr/`; they are **binding** and are
  referenced, not silently overridden (§17). A decision that changes an ADR requires a **new ADR** (status:
  supersedes), never an in-place rewrite of history.
- **RFCs** propose significant changes for discussion *before* implementation; substantial architectural
  changes start as an RFC, not as a surprise PR.

### 14.3 Inline Documentation & Comments

- **Docstrings** on non-trivial public APIs (§4.8). **Comments explain WHY**, never WHAT — if a comment is
  needed to explain what code does, the code is refactored instead (global standard).
- **No commented-out code.** Delete it; git remembers.

### 14.4 README Philosophy

- READMEs orient a newcomer: what this is, how to run it, where to look next. They point to the
  authoritative `docs/`, they do not duplicate it (single source of truth).

### 14.5 Keeping Documentation Synchronized

> ⚠️ **Documentation drift is a defect.** When code changes a documented behaviour, contract, or
> decision, the corresponding document is updated **in the same change**. Stale docs mislead humans and AI
> assistants alike and are treated as bugs in review (§18).

---

## 15. Security Standards

### 15.1 Secrets & Environment Variables

- **Secrets are never committed, never baked into images, never logged, never placed in URLs** (`10` §6,
  §12; `08` §12). They are injected at runtime via the settings/secret abstraction and validated at
  startup.

### 15.2 Input Validation

- **All external input is validated at the boundary and fails closed** (§5.3, §2.10). Untrusted input is
  never trusted into business logic.

### 15.3 Least Privilege

- Every credential, service account, and (future) role receives the **minimum access** needed (`08` §12.5,
  `10` §12.3). Broker credentials are scoped and isolated.

### 15.4 Dependencies

- Dependencies are **justified** (each is attack surface and maintenance burden), pinned, kept current,
  and scanned for known vulnerabilities in CI (`10` §12.6). New dependencies require reviewer agreement.

### 15.5 Sensitive Logging & Future Authentication

- Logging is scrubbed (§11.4). Authentication/authorization (JWT/OAuth/API-keys + RBAC) are **reserved
  seams** (`08` §4, `09` §12); code is written so enabling them requires no redesign, and the
  unauthenticated Phase 1 state is confined to trusted local use.

> **Architecture Callout — security is written in, not bolted on.** Boundary validation, secret hygiene,
> least privilege, and fail-closed defaults are coding habits enforced on every change — not a separate
> hardening phase.

---

## 16. Performance Standards

### 16.1 Memory & CPU

- Code is mindful of **allocation and copying** on hot paths; large data is streamed or paginated, never
  loaded wholesale (`08` §9.5). CPU-bound work is isolated off the event loop (§9.6).

### 16.2 Async & Concurrency

- Async discipline (§9) is the primary performance tool: non-blocking I/O and bounded concurrency keep the
  system responsive under load.

### 16.3 Caching

- Caching (Redis) is applied to **hot, expensive, and safely-cacheable** reads, with correctness owned
  explicitly and **never serving stale truth** (`08` §11.2, `10`). Cache is an optimization, never the
  source of truth.

### 16.4 Database Access

- Queries are **efficient and indexed** (`02`); N+1 access patterns are avoided; reads are paginated and
  bounded. The repository is the place to get this right once (§8).

### 16.5 Network Calls & Latency Awareness

- External calls are **minimized, timed out, and batched/coalesced** where appropriate (mirrors `09`
  §11.3). Every hot path has a **latency budget in mind**; p95/p99 matter more than averages (`08`/`09`).

### 16.6 Scalability Mindset

- Code is written to be **stateless and horizontally scalable** by default (`08` §3.3, `10` §13): no
  reliance on in-process state that would break across instances. Scale-out must remain a matter of
  configuration, not rewriting.

> ⚠️ **Measure before optimizing, but design for the budget.** Premature micro-optimization is
> discouraged (§2.3/§2.4); ignoring an obvious latency budget or an unbounded query is not optimization —
> it is a defect.

---

## 17. AI Development Guidelines

**This section is mandatory.** It governs all AI coding assistants — **Codex, ChatGPT, Claude, and any
future AI tool** — contributing to ApexScan. AI assistants are powerful contributors and are held to the
**same standards as human engineers, plus the additional constraints below.**

### 17.1 The AI Contract

| # | Rule for AI assistants |
|---|-------------------------|
| A1 | **AI must never redesign the architecture.** It implements within the boundaries of `00`–`10`; it does not invent new layers, patterns, or topologies. |
| A2 | **AI must follow the architecture documents.** When unsure, it reads `docs/` and conforms; it does not guess a structure. |
| A3 | **AI must not invent modules.** New modules/packages require a documented reason and human agreement; AI does not create speculative structure (§2.4). |
| A4 | **AI must not bypass dependency rules.** The Dependency Rule (§3.4) is inviolable — no inward layer imports an outward one, no engine imports strategies, no strategy imports brokers. |
| A5 | **AI must preserve ADR decisions.** Accepted ADRs are binding; AI never silently contradicts one. Changing a decision requires a new ADR proposed to humans (§14.2). |
| A6 | **AI-generated code requires human review.** No AI change merges without a human reviewer accountable for it (§18); AI output is a proposal, not an authority. |
| A7 | **AI must not duplicate logic.** It reuses existing utilities, services, and contracts rather than re-implementing them (§2.2). It searches before it writes. |
| A8 | **AI must respect the coding standards** in this document in full — style, typing, testing, logging, security. |
| A9 | **AI should produce production-quality code** — typed, tested, documented, and warning-free — not sketches or placeholders presented as finished. |
| A10 | **AI must not introduce business/trading logic** unless the task explicitly and legitimately calls for it within the correct layer; it never smuggles decisions into the transport, API, or engines that must stay fact-only (`06`/`07`/`09`). |
| A11 | **AI must surface uncertainty.** When a request appears to conflict with the architecture or an ADR, the AI **flags it and asks**, rather than proceeding on an assumption. |
| A12 | **AI must not fabricate.** It does not invent APIs, fields, flags, files, or data that do not exist; unverified claims are marked as such. |

### 17.2 Why AI Is Constrained This Way

AI assistants generate plausible code quickly — which is exactly why they need firm rails. The greatest
risk is not that an AI writes a syntax error (tooling catches that) but that it writes *architecturally
plausible* code that quietly violates a boundary, duplicates logic, or contradicts a decision. These rules
ensure AI **accelerates** the architecture instead of eroding it.

> ⚠️ **AI output is reviewed as untrusted until verified.** The reviewer — not the model — is accountable
> for every merged line. "The AI wrote it" is never a justification for a violation (§18, §19).

> **Architecture Callout — this document is the AI's charter.** An AI assistant that reads and obeys this
> manual and the `docs/` set can contribute safely at speed. The whole documentation effort (`00`–`11`)
> exists partly so that human and AI contributors share one source of truth about how ApexScan is built.

---

## 18. Code Review Standards

Review is where standards become reality. Every change — human or AI — is reviewed against the same
checklist before merge.

### 18.1 Review Checklist (per change)

| Dimension | The reviewer confirms… |
|-----------|------------------------|
| **Architecture compliance** | Layer boundaries and the Dependency Rule hold; no ADR is contradicted; no business logic in the wrong layer. |
| **Naming** | Names are clear, conventional, and intent-revealing. |
| **Performance** | No obvious latency-budget or query anti-patterns; hot paths are sane. |
| **Security** | No secrets, validated inputs, least privilege, scrubbed logs. |
| **Testing** | Behaviour is covered including edges/errors; critical paths tested; tests are meaningful. |
| **Documentation** | Docstrings present; docs updated in the same change; comments explain *why*. |
| **Maintainability** | Small units, single responsibility, no dead or commented-out code, no needless complexity. |
| **Error handling** | Failures modeled, nothing swallowed, no leaking internals. |

### 18.2 Review Discipline

- Reviews are **concrete**: issues cite `file:line`, present options with trade-offs when the fix isn't
  obvious, recommend one, and ask before large changes.
- **Automated checks run first** (lint, types, tests, security scan). Reviewers spend their attention on
  what tooling can't see — architecture, naming, and design.
- Reviewing is evaluated in order: **architecture → code quality → tests → performance**.

### 18.3 Approval Criteria

A change is approved only when **all** hold:

1. Green CI (lint, type-check, tests, security scan) — zero warnings.
2. Architecture and ADR compliance verified.
3. Meaningful tests for new/changed behaviour, including edges and errors.
4. Documentation updated in the same change.
5. No unresolved review threads.
6. A human reviewer is accountable (including for AI-authored changes — §17).

> ⚠️ **A review is an accountability transfer.** Approving a change means the reviewer vouches for it.
> Rubber-stamping — human or AI — is a process failure, not a shortcut.

---

## 19. Non-Negotiable Engineering Rules

These rules are **binding** on every contributor, human and AI. A change violating any of them does not
merge. A change that *needs* to violate one requires an ADR, not an exception in the moment.

### Architecture & Boundaries
| # | Rule |
|---|------|
| 1 | Dependencies point **inward**; no inner layer imports an outer one. |
| 2 | **No circular dependencies** — enforced in CI. |
| 3 | **No business logic inside the API layer.** |
| 4 | **Repositories never contain business logic.** |
| 5 | **Services reach persistence only through repositories.** |
| 6 | **Strategies never access brokers** (data reaches them only as facts). |
| 7 | **The Market Engine never knows about strategies.** |
| 8 | **The Market Engine computes facts, never decisions**; the Strategy Engine interprets, never measures. |
| 9 | **The transport/WebSocket layer never computes, scores, or re-ranks** (`09`). |
| 10 | **The frontend renders truth; it never re-computes or re-ranks results.** |
| 11 | **New behaviour extends seams; it never edits engines/core to add a strategy or broker** (Open/Closed). |
| 12 | **No duplicated business logic** — one authoritative implementation. |
| 13 | **Accepted ADRs are binding**; changing one requires a new superseding ADR. |
| 14 | **PostgreSQL is the source of truth; Redis is never authoritative.** |

### Code Quality
| # | Rule |
|---|------|
| 15 | **Zero warnings** from linters, type checkers, and compilers — fixed or explicitly justified. |
| 16 | Functions ≤100 lines; cyclomatic complexity ≤8. |
| 17 | ≤5 positional parameters; line length ≤100 chars. |
| 18 | **Absolute imports only**; no relative parent-traversal imports; no wildcard imports. |
| 19 | **No magic numbers/strings** in logic — named constants/enums. |
| 20 | **No globals and no import-time side effects.** |
| 21 | **No commented-out code**; dead code is removed, not deprecated. |
| 22 | **No premature abstraction** — extract on the third repetition, not the first. |
| 23 | **No speculative features/flags/config** without a real, current need. |
| 24 | Comments explain **why**, never **what**. |

### Python & Typing
| # | Rule |
|---|------|
| 25 | **Type hints are mandatory** on all public surfaces; strict type-checking passes. |
| 26 | **Google-style docstrings** on all non-trivial public APIs. |
| 27 | Immutable value objects for facts/results where applicable. |
| 28 | **Configuration is accessed only through the settings abstraction.** |
| 29 | **PEP 8** compliance via automated formatting/linting. |

### FastAPI & API
| # | Rule |
|---|------|
| 30 | **All input is validated at the boundary** before business logic (fail fast, fail closed). |
| 31 | Cross-cutting resources are supplied via **dependency injection**. |
| 32 | Responses use the **uniform shape** and the **single error model** (`08`). |
| 33 | The API is **explicitly versioned**; changes are additive within a major version. |
| 34 | Handlers contain no persistence access and no business decisions. |

### Frontend & TypeScript
| # | Rule |
|---|------|
| 35 | **Functional components and hooks only**; no class components. |
| 36 | **Client state and server state are separated** (store vs data layer). |
| 37 | **TypeScript strict mode**; no implicit `any`; nullability modeled. |
| 38 | Props are explicitly typed; contract types have a single source. |
| 39 | Presentational and container concerns are separated. |

### Async
| # | Rule |
|---|------|
| 40 | **Blocking calls never run on the event loop.** |
| 41 | Every awaitable is awaited or deliberately scheduled — no orphaned coroutines. |
| 42 | External calls carry **explicit timeouts**. |
| 43 | Concurrency is **bounded**; no unbounded fan-out. |
| 44 | Long-running work is cancellation-aware and shuts down gracefully. |

### Error Handling & Logging
| # | Rule |
|---|------|
| 45 | **Exceptions are never swallowed**; catch to handle, not to silence. |
| 46 | Errors **fail fast** with actionable, contextual messages. |
| 47 | **Internals never leak** to clients (no stack traces, queries, secrets, internal ids). |
| 48 | Logs are **structured** and carry **correlation ids**. |
| 49 | **Secrets and PII are never logged.** |
| 50 | Retries apply only to transient, idempotent operations, with bounded backoff + jitter. |

### Testing
| # | Rule |
|---|------|
| 51 | New/changed behaviour ships with **meaningful tests**, including edges and errors. |
| 52 | **Test behaviour, not implementation**; mock only boundaries. |
| 53 | Deterministic engines have **replay tests**. |
| 54 | Critical paths must be covered; uncovered critical paths block merge. |

### Git & Process
| # | Rule |
|---|------|
| 55 | **No direct commits/pushes to the default branch**; changes go through reviewed PRs. |
| 56 | **Never rewrite shared history**; never force-push a shared branch. |
| 57 | Commits are atomic, imperative, ≤72-char subject, one logical change. |
| 58 | Merge requires **green CI and approving review**; no self-merge of unreviewed code. |
| 59 | Releases are semantically **versioned and tagged**. |

### Security
| # | Rule |
|---|------|
| 60 | **Secrets are never committed, imaged, logged, or placed in URLs.** |
| 61 | **All external input is validated and fails closed.** |
| 62 | **Least privilege** for every credential and (future) role. |
| 63 | New dependencies are justified, pinned, and vulnerability-scanned. |

### Documentation, AI & Review
| # | Rule |
|---|------|
| 64 | **Documentation is updated in the same change** that alters documented behaviour — drift is a defect. |
| 65 | Every **public function is documented**. |
| 66 | **AI assistants follow this manual and the architecture docs**; AI never redesigns architecture or contradicts an ADR. |
| 67 | **AI-generated code requires accountable human review**; "the AI wrote it" is never a justification. |
| 68 | **AI must not invent modules, APIs, fields, or files**, and must surface uncertainty rather than assume. |
| 69 | Every merged change satisfies the **code-review approval criteria** (§18.3). |

---

## 20. Engineering Checklist

Grouped by category. Every box is a per-change or per-repository commitment.

### Architecture
- [ ] The change respects the layered architecture (`01`/`03`).
- [ ] Dependencies point inward only.
- [ ] No circular dependencies are introduced.
- [ ] No new module/package is invented without a documented reason.
- [ ] No business logic sits in the API/transport/frontend layers.
- [ ] Repositories contain no business logic.
- [ ] Strategies do not access brokers; the Market Engine does not know strategies.
- [ ] The transport does not compute, score, or re-rank.
- [ ] New behaviour extends existing seams (Open/Closed).
- [ ] No accepted ADR is contradicted.
- [ ] Structure supports growth to many strategies/brokers.
- [ ] Each module has a single, documented responsibility.
- [ ] The event boundary is respected (producers publish; consumers subscribe).

### Python
- [ ] PEP 8 compliant via automated formatting.
- [ ] Type hints on all public surfaces; strict type-check passes.
- [ ] Google-style docstrings on non-trivial public APIs.
- [ ] Naming is conventional and intent-revealing.
- [ ] No magic numbers/strings; constants/enums used.
- [ ] No globals; no import-time side effects.
- [ ] Immutable value objects used for facts/results where applicable.
- [ ] Configuration accessed only via the settings abstraction.
- [ ] Zero linter/type warnings (or justified suppressions).
- [ ] Functions ≤100 lines, complexity ≤8, ≤5 positional params.
- [ ] No wildcard imports; imports grouped and ordered by tooling.

### FastAPI
- [ ] Endpoints grouped by category and mounted under a version.
- [ ] Cross-cutting resources injected via DI.
- [ ] Input validated at the boundary before business logic.
- [ ] Handlers delegate to services and contain no business logic.
- [ ] Services use repositories only for persistence.
- [ ] Responses use the uniform shape and single error model.
- [ ] Versioning is additive within a major version.
- [ ] Authorization (future) is enforced before the service is called.
- [ ] No endpoint returns an unbounded collection.

### React
- [ ] Functional components and hooks only.
- [ ] One component per file; filename matches component.
- [ ] Client state and server state are kept separate.
- [ ] State owned at the lowest component that needs it.
- [ ] Props explicitly typed and minimal.
- [ ] Composition preferred over flag-heavy components.
- [ ] Presentational vs container concerns separated.
- [ ] Components render server truth without re-computing/re-ranking.
- [ ] Custom hooks encapsulate reusable stateful logic with clear names.
- [ ] The rules of hooks are respected everywhere.

### TypeScript
- [ ] Strict mode on; no implicit `any`.
- [ ] Nullability modeled honestly.
- [ ] `type` vs `interface` follows the documented convention.
- [ ] Immutable/`readonly` shapes preferred for data and props.
- [ ] Enums/union literals used for fixed value sets.
- [ ] Generics/utility types used instead of duplication.
- [ ] Contract types have a single source of truth.
- [ ] `any` appears only at a justified, commented boundary.
- [ ] Naming mirrors backend concepts where types cross the wire.

### Database
- [ ] All persistence goes through repositories.
- [ ] Atomic operations are wrapped in transactions at the service boundary.
- [ ] Read/write paths are distinguished appropriately.
- [ ] Connections are injected and always released.
- [ ] Schema changes ship as versioned, reviewed migrations.
- [ ] Migrations are never hand-applied to a running database.
- [ ] Naming follows the documented convention.
- [ ] Cache values are never treated as authoritative.
- [ ] No raw queries escape the repository layer.
- [ ] Migrations are reversible where feasible and reviewed.

### Async
- [ ] I/O paths are async; no blocking on the event loop.
- [ ] Every awaitable is awaited or deliberately scheduled.
- [ ] External calls have explicit timeouts.
- [ ] Concurrency is bounded.
- [ ] Long-running work is cancellation-aware.
- [ ] Background work is observable and shuts down gracefully.
- [ ] Event boundaries are used where they are the right seam.
- [ ] Shared mutable state across tasks is avoided or synchronized.
- [ ] No async operation can wait forever.

### Error Handling
- [ ] Expected failures are modeled through the uniform error model.
- [ ] Unexpected failures fail fast and log with context + correlation id.
- [ ] No exception is swallowed.
- [ ] No internals leak to clients.
- [ ] Retries apply only to transient, idempotent operations with bounded backoff.
- [ ] Recovery paths are explicit and tested.
- [ ] User-facing errors are safe and carry a correlation id.
- [ ] Each failure is logged once with context, not at every layer.
- [ ] Validation errors are precise about what and where.

### Logging
- [ ] Logs are structured, not free-form.
- [ ] Log levels are used consistently.
- [ ] Correlation ids thread through logs.
- [ ] No secrets or PII are logged.
- [ ] Audit-worthy state changes are logged.
- [ ] Performance/latency signals are logged where budgeted.
- [ ] Broker and strategy execution logs exist without leaking sensitive data.
- [ ] No ad-hoc print statements are committed.
- [ ] Log volume/retention is bounded and rotated (`10`).

### Testing
- [ ] New/changed behaviour has meaningful tests.
- [ ] Edges and error paths are tested, not just the happy path.
- [ ] Only boundaries are mocked.
- [ ] Contract tests protect API/event consumers.
- [ ] Deterministic engines have replay tests.
- [ ] Regression tests lock in fixed bugs.
- [ ] Performance-sensitive paths have budget tests.
- [ ] Test names describe scenario and expected outcome.
- [ ] Critical paths are covered; uncovered critical paths block merge.
- [ ] Tests fail when the code is broken (verified, not assumed).
- [ ] Cancellation and timeout paths are tested.

### Security
- [ ] No secrets are committed, imaged, logged, or in URLs.
- [ ] All external input is validated and fails closed.
- [ ] Least privilege applies to credentials and roles.
- [ ] Dependencies are justified, pinned, and scanned.
- [ ] Logs are scrubbed of sensitive data.
- [ ] Auth/authz seams are respected (no redesign to enable them later).
- [ ] TLS/HTTPS and CORS boundaries are not weakened.
- [ ] Security controls fail closed by default.

### Performance
- [ ] Hot paths avoid needless allocation/copying.
- [ ] Large data is streamed/paginated, never loaded wholesale.
- [ ] CPU-bound work is off the event loop.
- [ ] Caching is correct and never serves stale truth.
- [ ] Queries are efficient/indexed; no N+1 patterns.
- [ ] External calls are minimized, timed out, and coalesced where sensible.
- [ ] Hot paths respect a latency budget (p95/p99 mindset).
- [ ] Code is stateless and horizontally scalable by default.
- [ ] Optimizations are measured, not guessed.
- [ ] Fan-out and concurrency are bounded (`09`).

### Git
- [ ] Work is on a correctly named branch, never the default branch.
- [ ] Commits are atomic, imperative, and well-described.
- [ ] The PR describes what the code does now, in plain language.
- [ ] CI is green (lint, types, tests, security scan) with zero warnings.
- [ ] Review is approved and threads resolved before merge.
- [ ] Shared history is never rewritten.
- [ ] Releases are semantically versioned and tagged.
- [ ] Hotfixes are reviewed, tested, tagged, and merged back.
- [ ] No secrets are ever committed.

### Documentation
- [ ] Public functions are documented.
- [ ] Comments explain why, not what; no commented-out code.
- [ ] Documentation is updated in the same change as the behaviour.
- [ ] ADRs are added/superseded (never rewritten) for decisions.
- [ ] Significant changes start as an RFC before implementation.
- [ ] READMEs point to authoritative docs without duplicating them.
- [ ] Docstrings state purpose, params, returns, and raised errors.
- [ ] Cross-references between docs stay accurate after changes.

### AI Usage
- [ ] AI-generated code follows this manual in full.
- [ ] AI did not redesign architecture or invent modules/APIs/files.
- [ ] AI did not bypass dependency rules or contradict an ADR.
- [ ] AI did not duplicate existing logic.
- [ ] AI output is production-quality (typed, tested, documented, warning-free).
- [ ] AI surfaced uncertainty instead of assuming.
- [ ] A human reviewer is accountable for the AI-authored change.
- [ ] AI preserved every relevant ADR decision.
- [ ] AI did not introduce fact-only-boundary violations (`06`/`07`/`09`).

### Deployment
- [ ] The change is compatible with immutable, containerized deployment (`10`).
- [ ] No environment-specific values or secrets are baked into code/images.
- [ ] Configuration is externalized and validated at startup.
- [ ] Health/readiness behaviour is preserved.
- [ ] The change is rollback-safe (no irreversible one-way migration without a plan).
- [ ] Backward compatibility is maintained within a major version.
- [ ] The change works identically across environments (config-only differences).
- [ ] No development-only convenience leaks into production paths.

### Code Review
- [ ] Architecture compliance verified.
- [ ] Naming, performance, and security reviewed.
- [ ] Tests are meaningful and cover edges/errors.
- [ ] Documentation is present and synchronized.
- [ ] Maintainability confirmed (small units, no dead code, no needless complexity).
- [ ] All approval criteria (§18.3) are met before merge.
- [ ] Issues are cited concretely with file:line and options/trade-offs.
- [ ] Automated checks passed before human review time is spent.

---

## 21. Summary

### 21.1 What This Document Is

`11_CODING_GUIDELINES.md` is the **Engineering Standards Manual** for ApexScan — the single, binding
source of *how* code is written, reviewed, tested, and merged. It translates the architecture in `00`–`10`
into concrete engineering discipline: principles that provide reasoning, standards per language and layer,
testing and logging and security practice, git and documentation process, explicit guidelines for AI
assistants, a review contract, **69 non-negotiable rules**, and a **grouped engineering checklist** that
makes the standards actionable on every change.

### 21.2 What It Owns and What It Never Owns

| Owns | Never Owns |
|------|------------|
| How code is written, styled, and typed | What the software does (owned by `00`–`10`) |
| Testing, logging, error, and security practice | Business/trading logic or strategy rules |
| Git, review, and documentation process | The API/event/data contracts themselves (`02`/`08`/`09`) |
| The AI-assistant contract | The deployment topology (`10`) |
| The engineering rules and checklist | Architectural decisions (owned by ADRs) |

### 21.3 Relationship to the Rest of the Docs

This manual sits *across* the entire architecture: it enforces the boundaries defined in `01`/`03`, the
engine discipline in `06`/`07`, the contracts in `08`/`09`, the operations in `10`, and the decisions in
`docs/adr/`. It is the connective tissue that keeps many contributors — human and AI — building **one**
coherent system.

### 21.4 Engineering Readiness Assessment

| Dimension | Status | Notes |
|-----------|--------|-------|
| Engineering principles | ✅ Ready | §2; reasoning tools with purpose/benefit/when/mistakes. |
| Repository & dependency discipline | ✅ Ready | §3; Dependency Rule, no cycles, absolute imports. |
| Language standards (Python/TS) | ✅ Ready | §4/§7; typing, strict mode, naming, immutability. |
| Layer standards (FastAPI/React/DB) | ✅ Ready | §5/§6/§8; boundaries and patterns explicit. |
| Async, error, logging | ✅ Ready | §9/§10/§11; async-first, fail-fast, structured/correlated. |
| Testing | ✅ Ready | §12; behaviour-first, replay for determinism, edges/errors. |
| Git, docs, security, performance | ✅ Ready | §13–§16; process and practice defined. |
| AI development contract | ✅ Ready | §17; explicit, binding rules for Codex/ChatGPT/Claude/future. |
| Code review | ✅ Ready | §18; checklist + approval criteria + accountability. |
| Rules & checklist | ✅ Ready | §19 (69 rules) + §20 (grouped checklist). |

**Why this document is sufficient for a team of developers and AI assistants to build ApexScan
consistently while preserving the architecture:** it makes the architecture's boundaries **enforceable at
the level of a single change**. A new engineer or an AI assistant does not need to rediscover how ApexScan
is meant to be built — the principles explain the reasoning, the per-layer standards give the concrete
conventions, the non-negotiable rules draw the hard lines, the checklist operationalizes them per change,
and the review contract (with explicit AI constraints and human accountability) ensures nothing merges
that violates them. Combined with the architecture documents (`00`–`10`) that define *what* the system is
and the ADRs that record *why* decisions were made, this manual defining *how* code is written completes
the set: **any contributor, human or AI, can now build ApexScan at speed without eroding its
architecture.**

---

*End of `11_CODING_GUIDELINES.md` — Official Engineering Standards Manual for ApexScan.*
