# ApexScan Project Overview

> **Document status:** Official — Phase 2 baseline
> **Owner:** Platform Architecture
> **Audience:** Engineering, Product, QA, DevOps
> **Related documents:** `01_SYSTEM_ARCHITECTURE.md` → `12_ROADMAP.md`

This document is the canonical, authoritative overview of the ApexScan
platform. It defines what we are building, why, the boundaries of the current
version, and the principles every contributor is expected to follow. All
other design documents in `docs/` elaborate on the sections summarised here.

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Vision](#2-vision)
3. [Project Goals](#3-project-goals)
4. [Project Scope](#4-project-scope)
5. [Technology Stack](#5-technology-stack)
6. [High Level Modules](#6-high-level-modules)
7. [Design Principles](#7-design-principles)
8. [Development Workflow](#8-development-workflow)
9. [Project Folder Structure](#9-project-folder-structure)
10. [Coding Standards](#10-coding-standards)
11. [Non Functional Requirements](#11-non-functional-requirements)
12. [Future Roadmap](#12-future-roadmap)
13. [Assumptions](#13-assumptions)
14. [Risks](#14-risks)
15. [Success Criteria](#15-success-criteria)

---

## 1. Introduction

### 1.1 What is ApexScan?

**ApexScan** is a professional, real-time trading **scanner** platform. Its
purpose is to continuously evaluate large universes of financial instruments
against a growing library of quantitative strategies and surface — in
real time — the instruments that currently match a trader's chosen criteria.

A *scanner* is distinct from a *trading bot*. ApexScan answers the question
**"which instruments are interesting right now, and why?"** It does not, in
its current version, place orders or manage positions. It transforms raw
market data into ranked, explainable, actionable signals and presents them
through a fast, dense, trader-grade dashboard.

The platform is architected from day one to be **broker-independent**,
**strategy-independent**, and **horizontally scalable**, so that the same
core engine can drive three strategies today and hundreds across multiple
brokers and asset classes tomorrow.

> **📌 Architecture callout — Scanner, not executor.**
> Keeping scanning and execution as separate concerns is a deliberate
> architectural boundary. It lets us harden the data and signal pipeline in
> isolation before introducing the operational and financial risk of order
> placement. Live trading is a *future* layer that will consume the same
> signal stream, not a rewrite of it.

### 1.2 Why this project exists

Serious discretionary and systematic traders face a recurring problem: the
tools available to them are either **too rigid** (closed platforms with a
fixed set of built-in scans that cannot be extended) or **too raw** (spreadsheets,
notebooks, and ad-hoc scripts that do not scale, are hard to operate, and
break silently in live markets).

Existing solutions typically suffer from one or more of the following:

- **Broker lock-in.** A scan written for one broker's data feed cannot be
  reused when the trader moves to another broker or market.
- **No extensibility.** Adding a new strategy requires vendor cooperation or
  is simply impossible.
- **Poor real-time behaviour.** Results are delayed, polled, or refreshed on a
  slow timer rather than pushed as the market moves.
- **No separation of concerns.** Data acquisition, strategy logic, and
  presentation are tangled together, making the system fragile and untestable.

ApexScan exists to close this gap: a platform where **strategies are plug-ins**,
**brokers are adapters**, and **results stream live** to a professional UI —
all built on a maintainable, testable, production-grade foundation.

### 1.3 Project objectives

| # | Objective | Description |
|---|-----------|-------------|
| O1 | **Real-time scanning** | Evaluate instruments continuously and push matches with minimal latency. |
| O2 | **Extensibility** | Add new strategies and brokers without modifying the core engine. |
| O3 | **Explainability** | Every match carries the reason it matched, not just a boolean. |
| O4 | **Operability** | Run reliably as a containerised service with health, logging, and monitoring built in. |
| O5 | **Scalability path** | Start on a single node; scale to many strategies, instruments, and brokers without re-architecture. |
| O6 | **Developer velocity** | A clean, well-documented codebase where new contributors are productive quickly. |

---

## 2. Vision

ApexScan's long-term vision is to become a **universal, extensible market
intelligence platform** — the layer between raw market data and trader
decision-making, regardless of broker, exchange, or asset class.

The following capabilities describe the destination. They are **not** all in
scope for Version 1 (see [Section 4](#4-project-scope)); they define the
trajectory the architecture must never foreclose.

| Capability | Long-term intent |
|------------|------------------|
| **Multiple brokers** | Dhan, Zerodha, Binance and others plugged in as interchangeable adapters behind one interface. |
| **Multiple exchanges** | NSE, BSE, MCX, global crypto and equity venues, normalised into a common instrument model. |
| **Multiple asset classes** | Equities, futures, options, currencies, commodities, and crypto. |
| **Multiple strategies** | A library scaling to 100+ concurrent, independently maintained strategies. |
| **Real-time scanning** | Event-driven evaluation as ticks and candles arrive, not periodic polling. |
| **Paper trading** | Simulated order placement against live signals to validate strategies risk-free. |
| **Live trading** | Real order execution through broker adapters, governed by risk controls. |
| **Backtesting** | Replay of historical data through the same strategy code that runs live. |
| **Strategy marketplace** *(future)* | A curated ecosystem where strategies can be shared, versioned, and subscribed to. |
| **AI-assisted analysis** *(future)* | Machine-assisted signal ranking, anomaly detection, and natural-language market summaries. |

> **💡 Tip — Design for the destination, build for the increment.**
> The vision list is long, but each item maps to a seam already present in the
> Phase 1 architecture (adapters, strategy plug-ins, event flow). We build one
> increment at a time, but we never build a wall across a future seam.

> **⚠️ Warning — Vision is not a commitment schedule.**
> Nothing in this section is a delivery promise for a specific date. Scope and
> sequencing are governed by [Section 4](#4-project-scope) and
> [Section 12](#12-future-roadmap), and by explicit product decisions.

---

## 3. Project Goals

The goals below are the **quality attributes** that shape every technical
decision. When a trade-off arises, these are the tie-breakers.

### 3.1 Modular
The system is decomposed into cohesive modules with well-defined
responsibilities and interfaces. A change inside one module (e.g. how candles
are aggregated) must not ripple into unrelated modules (e.g. the UI).

### 3.2 Extensible
Adding a **strategy** or a **broker** is an act of *addition*, not
*modification*. New strategies register as plug-ins; new brokers implement a
shared adapter contract. The core never needs to know their names.

### 3.3 High Performance
The scanning path is optimised for throughput and low latency. The platform is
**async-first** so that thousands of concurrent I/O operations (market data,
cache, database, WebSocket clients) do not block one another.

### 3.4 Event Driven
Market events (ticks, candle closes) drive computation. The system reacts to
data as it arrives rather than polling on a timer, which reduces latency and
wasted work.

### 3.5 Broker Independent
No core module depends on a specific broker's API. Broker specifics live
entirely inside adapters and are normalised at the boundary.

### 3.6 Strategy Independent
The engine treats strategies as black boxes conforming to a contract. It knows
how to *drive* a strategy, not what any particular strategy *does*.

### 3.7 Easy Maintenance
Clear layering, strong typing, comprehensive documentation, and tests make the
system safe to change. A maintainer can reason about a module in isolation.

### 3.8 Production Ready
From day one the platform is containerised, configuration-driven, observable
(structured logging, health checks), and free of global mutable state — ready
to be deployed and operated, not merely demonstrated.

> **📌 Architecture callout — Two seams above all.**
> The two most important architectural seams are the **Broker Adapter
> interface** and the **Strategy contract**. Every other goal is easier to
> honour if these two boundaries stay clean. Guard them in code review.

---

## 4. Project Scope

This section defines the **hard boundary** of Version 1 (V1). Anything not
explicitly listed as included is out of scope for V1.

### 4.1 Included in Version 1

| ✔ | Item | Notes |
|---|------|-------|
| ✔ | **Open = High Strategy** | Scan for instruments whose opening price equals the day's high (a bearish/exhaustion signal pattern). |
| ✔ | **Open = Low Strategy** | Scan for instruments whose opening price equals the day's low (a bullish/strength signal pattern). |
| ✔ | **Narrow CPR Strategy** | Scan for instruments exhibiting a narrow Central Pivot Range, indicating potential trending days. |
| ✔ | **Scanner Dashboard** | Real-time web UI presenting live scan results in a dense, sortable grid with charts. |
| ✔ | **PostgreSQL** | Durable storage for instruments, strategy configuration, and scan metadata. |
| ✔ | **Redis** | Caching and the real-time fan-out backbone. |
| ✔ | **FastAPI** | Async backend serving the REST API and WebSocket channel. |
| ✔ | **React** | The frontend single-page application. |

> **📝 Note — Strategy names describe outcomes, not code.**
> The three V1 strategies are listed here for scope clarity. Their algorithms
> are specified in `07_STRATEGY_ENGINE.md` and implemented in Phase 2
> development. This overview intentionally contains no strategy logic.

### 4.2 Explicitly NOT included in Version 1

| ❌ | Item | Reason for exclusion |
|---|------|---------------------|
| ❌ | **Live Trading** | Order execution carries financial and regulatory risk; deferred until the signal pipeline is proven. |
| ❌ | **Paper Trading** | Depends on an execution simulation layer not yet built. |
| ❌ | **Backtesting** | Requires a historical data pipeline and replay engine planned for a later version. |
| ❌ | **AI features** | Deferred until a stable stream of labelled signals exists to build on. |
| ❌ | **ML models** | Same rationale as AI; no training data or serving infrastructure in V1. |
| ❌ | **Strategy Marketplace** | A distribution/monetisation concern that presupposes a mature strategy ecosystem. |
| ❌ | **Multi-broker (live)** | The adapter interface exists, but V1 validates the pipeline with a single data source. |

> **⚠️ Warning — Scope creep is the primary schedule risk.**
> Requests to "just add" execution, backtesting, or AI to V1 must be routed
> through product prioritisation and the roadmap in
> [Section 12](#12-future-roadmap). The architecture *anticipates* these; V1
> deliberately *excludes* them to ship a reliable core first.

---

## 5. Technology Stack

The stack is chosen for developer productivity, async performance, strong
typing across the full stack, and operational maturity.

| Layer | Technology | Role |
|-------|-----------|------|
| **Backend** | Python 3.13+, FastAPI, Pydantic v2, asyncio, uvicorn | Async API server, validation, and the scanning runtime. |
| **Backend — ORM** | SQLAlchemy 2.0 (async), Alembic | Data access and schema migrations. |
| **Frontend** | React 19, TypeScript, Vite | Component-based SPA with a fast build/dev toolchain. |
| **Frontend — state** | Zustand, TanStack Query | Client UI state and server-state caching, kept distinct. |
| **Frontend — routing/UI** | React Router, Tailwind CSS | Navigation and utility-first styling. |
| **Frontend — data & charts** | AG Grid Community, TradingView Lightweight Charts | High-density result grids and price charts. |
| **Database** | PostgreSQL | Durable relational store for instruments, config, and metadata. |
| **Cache** | Redis | Caching, ephemeral state, and pub/sub real-time fan-out. |
| **Infrastructure** | Docker, Docker Compose, Nginx *(prepared)* | Reproducible environments and reverse proxy for production. |
| **Testing** | pytest, pytest-asyncio, httpx (backend); type-checking and lint tooling (frontend) | Automated verification at unit and integration levels. |
| **Quality tooling** | Ruff, mypy (backend); ESLint, `tsc` (frontend) | Linting, formatting, and static type checking. |
| **Deployment** | Docker images orchestrated via Compose; Nginx reverse proxy | Containerised delivery; production wiring prepared. |

> **💡 Tip — Typed end to end.**
> Pydantic on the backend and TypeScript on the frontend give us validation and
> type safety on both sides of the wire. Keeping API schemas and TS types in
> agreement is a first-class maintenance concern, documented in
> `08_API_SPECIFICATION.md`.

---

## 6. High Level Modules

ApexScan is composed of the following modules. Each is described by its
**responsibility**, its **key collaborators**, and its **boundary** (what it
must *not* do). Detailed designs live in the referenced documents.

### 6.1 Frontend
**Responsibility:** Present live scan results and market context to the trader
through a fast, dense, professional dashboard.
**Collaborators:** REST API (configuration, historical reads), WebSocket
channel (live updates).
**Boundary:** Contains no trading or scanning logic. It renders state and
issues intent; all computation happens server-side.
*See `04_FRONTEND_ARCHITECTURE.md`.*

### 6.2 Backend
**Responsibility:** Host the API, coordinate the scanning runtime, and enforce
application logic through a service layer.
**Collaborators:** All backend modules; PostgreSQL; Redis.
**Boundary:** The API layer stays thin; business logic lives in services, data
access in repositories.
*See `03_BACKEND_ARCHITECTURE.md`.*

### 6.3 Market Engine
**Responsibility:** Acquire market data through broker adapters, manage
subscriptions, aggregate candles, and drive the scan loop that feeds
strategies with normalised data.
**Collaborators:** Broker Adapters (upstream), Strategy Engine (downstream),
Redis (state/fan-out).
**Boundary:** Knows nothing about specific brokers (only the adapter contract)
or specific strategies (only how to dispatch data).
*See `06_MARKET_ENGINE.md`.*

### 6.4 Strategy Engine
**Responsibility:** Register, enable/disable, and dispatch market data to the
strategy plug-ins; collect and rank their outputs.
**Collaborators:** Market Engine (input), individual strategies (plug-ins),
persistence and WebSocket layers (output).
**Boundary:** Treats each strategy as a contract-conforming black box. Contains
no strategy-specific mathematics itself.
*See `07_STRATEGY_ENGINE.md`.*

### 6.5 Database
**Responsibility:** Durably store instruments, strategy configuration, scan
runs, and results metadata.
**Collaborators:** Repositories (the only code that touches it directly),
Alembic (schema evolution).
**Boundary:** No business logic in the database layer beyond integrity
constraints; behaviour lives in services.
*See `02_DATABASE_DESIGN.md`.*

### 6.6 Redis
**Responsibility:** Provide caching for hot data and a pub/sub backbone for
broadcasting live results to connected clients.
**Collaborators:** Market Engine, Strategy Engine, WebSocket layer.
**Boundary:** Ephemeral by design; nothing that must survive a restart lives
only in Redis.

### 6.7 Broker Adapter
**Responsibility:** Translate between ApexScan's internal model and a specific
broker's API — authentication, market data, and instrument metadata — and
normalise responses into internal schemas.
**Collaborators:** Market Engine (consumer), external broker APIs.
**Boundary:** All broker-specific quirks are contained here and nowhere else.
Adding a broker means adding an adapter.
*See `05_DATA_PROVIDER.md`.*

### 6.8 API
**Responsibility:** Expose a versioned REST contract for configuration,
control, and historical reads.
**Collaborators:** Services (via dependency injection), frontend.
**Boundary:** Thin and declarative; delegates all work to services.
*See `08_API_SPECIFICATION.md`.*

### 6.9 WebSocket
**Responsibility:** Maintain client connections and stream live scan results
and market updates, backed by Redis pub/sub.
**Collaborators:** Redis, Strategy Engine, frontend.
**Boundary:** A transport layer; it broadcasts state, it does not compute it.
*See `09_WEBSOCKET_FLOW.md`.*

### 6.10 Documentation
**Responsibility:** Maintain the living design record (`docs/`) that governs the
system's architecture and standards.
**Collaborators:** Every engineer.
**Boundary:** Documentation describes the system as it *is* and as it is
*intended to be*, and is updated alongside the code it describes.

> **📌 Architecture callout — The dependency arrow points inward.**
> Frontend → API → Services → Repositories/Adapters → Data stores. The core
> domain (services, engine, strategy contract) depends on **nothing** outward.
> Brokers and the UI are replaceable details at the edges.

---

## 7. Design Principles

These principles are non-negotiable. Code review enforces them.

### 7.1 SOLID
- **Single Responsibility** — each module/class has one reason to change.
- **Open/Closed** — extend behaviour by adding strategies/adapters, not by
  editing the core.
- **Liskov Substitution** — any adapter or strategy is usable wherever its
  interface is expected.
- **Interface Segregation** — small, focused contracts (a strategy contract, an
  adapter contract) rather than one god-interface.
- **Dependency Inversion** — the engine depends on abstractions (interfaces),
  not on concrete brokers or strategies.

### 7.2 DRY (Don't Repeat Yourself)
Shared behaviour is factored into base classes and utilities *once it has
earned reuse*. We avoid duplication, but we also avoid premature abstraction —
a pattern is extracted after it recurs, not in anticipation.

### 7.3 KISS (Keep It Simple)
Prefer the simplest design that satisfies the requirement. Complexity must be
justified by a concrete, present need — never by a hypothetical future one.

### 7.4 Repository Pattern
All persistence goes through repositories. Services depend on repository
interfaces, never on the ORM session directly. This isolates the domain from
storage details and makes data access testable.

### 7.5 Dependency Injection
Collaborators (settings, database sessions, cache clients) are injected, not
constructed inline or read from globals. The composition happens at the
application boundary, keeping the rest of the code pure and testable.

### 7.6 Service Layer
Business logic lives in a dedicated service layer that orchestrates
repositories, adapters, and engines. The API calls services; services call
everything else. This keeps controllers thin and logic reusable.

### 7.7 Clean Architecture
The system is layered so that dependencies point inward toward the domain.
Frameworks (FastAPI, SQLAlchemy), brokers, and the UI are outer-ring details
that can be replaced without touching the core.

### 7.8 Async Programming
The backend is async-first. All I/O-bound work — market data, database, cache,
WebSocket — is non-blocking, allowing a single process to handle high
concurrency efficiently.

> **⚠️ Warning — No global mutable state.**
> Global singletons for configuration or connections are a recurring source of
> hidden coupling and test flakiness. Configuration and connections are
> provided through injection. This rule is strict.

---

## 8. Development Workflow

ApexScan is delivered in a deliberate, dependency-ordered sequence. Each stage
produces a stable foundation the next stage relies on.

```
Infrastructure
      ↓
Architecture Documents
      ↓
Database
      ↓
Market Engine
      ↓
Strategy Engine
      ↓
Frontend
      ↓
Testing
      ↓
Deployment
```

### 8.1 Why this order

| Stage | Why it comes when it does |
|-------|---------------------------|
| **Infrastructure** | Nothing can be built or run without a reproducible environment (containers, config, wiring). This is Phase 1, now complete. |
| **Architecture Documents** | Design decisions are made and recorded *before* code, so the team builds against a shared, reviewed plan rather than discovering the design by accident. |
| **Database** | The data model is the contract many modules depend on. Defining it early prevents costly downstream rework. |
| **Market Engine** | Strategies are meaningless without a reliable, normalised stream of market data to consume. Data acquisition must exist first. |
| **Strategy Engine** | With data flowing, the engine that registers and drives strategies can be built and validated against real inputs. |
| **Frontend** | Once the backend produces real signals, the UI has genuine data to render — avoiding UI built against mocks that later diverge. |
| **Testing** | Verification is continuous, but a dedicated hardening pass ensures behaviour, edges, and failure paths are covered before release. |
| **Deployment** | The final step packages and ships a system that has already been designed, built, and verified. |

> **📝 Note — "Testing last" is a stage name, not a strategy.**
> Tests are written *alongside* every stage. The final "Testing" stage is a
> dedicated hardening and integration pass, not the first time tests appear.
> See [Section 10](#10-coding-standards).

> **💡 Tip — Documents evolve with code.**
> The architecture documents are living. When implementation reveals a better
> design, we update the document in the same change — the docs never drift into
> fiction.

---

## 9. Project Folder Structure

The repository root is organised into six top-level areas, each with a single
clear purpose.

| Folder | Purpose |
|--------|---------|
| `backend/` | The FastAPI application and all server-side code, organised by clean-architecture layers (api, services, repositories, models, adapters, engines, cache, database). Also hosts migrations and backend tests. |
| `frontend/` | The React + TypeScript single-page application, organised by concern (components, pages, layouts, hooks, services, store, routes, types, styles). |
| `docs/` | The living design record — this document and its companions (`01`–`12`). The source of truth for architecture and standards. |
| `docker/` | Build and runtime configuration kept out of the application tree: Dockerfiles' supporting configs, the Nginx reverse-proxy config, and database bootstrap scripts. |
| `scripts/` | Developer and operational helper scripts (e.g. bringing up the local stack) that automate common tasks. |
| `tests/` | Cross-cutting and end-to-end test suites that span both backend and frontend, distinct from the unit/integration tests co-located with each service. |

> **📌 Architecture callout — Structure mirrors the layering.**
> The folder layout is not cosmetic; it *enforces* the architecture. Adapters
> live under `backend/app/adapters/`, strategies under
> `backend/app/strategies/`, and the engine under `backend/app/market_engine/`
> and `backend/app/strategy_manager/`. The physical structure makes the
> logical boundaries visible and hard to violate by accident.

---

## 10. Coding Standards

Standards exist so that any engineer can read, change, and trust any part of
the codebase. Detailed rules live in `11_CODING_GUIDELINES.md`; the essentials
follow.

### 10.1 Python
- **PEP 8** compliance, enforced by the linter.
- **Full type hints** on all public functions; static type checking is part of
  the build.
- **Google-style docstrings** on non-trivial public APIs.
- **Async-first** for all I/O.
- **No global mutable state**; dependencies are injected.
- Functions stay small and single-purpose; complexity is kept low.

### 10.2 TypeScript
- **Strict mode** enabled; no implicit `any`.
- **Typed API boundaries** — server responses are typed and validated at the
  edge.
- **Separation of state** — server state via TanStack Query, client/UI state
  via Zustand; the two are not conflated.
- Components are presentational where possible; data-fetching lives in hooks.

### 10.3 Git
- **Trunk-protected:** never push directly to the main branch; all changes land
  through pull requests.
- **One logical change per commit.**
- History on shared branches is never rewritten.
- Secrets are never committed; configuration is provided via environment files
  that are excluded from version control.

### 10.4 Branch Naming
A short, descriptive, kebab-case name prefixed by change type:

| Prefix | Use for |
|--------|---------|
| `feature/` | New functionality (e.g. `feature/narrow-cpr-strategy`). |
| `fix/` | Bug fixes. |
| `docs/` | Documentation-only changes. |
| `chore/` | Tooling, dependencies, housekeeping. |
| `refactor/` | Behaviour-preserving restructuring. |

### 10.5 Commit Message Format
- **Imperative mood**, subject line ≤ 72 characters
  (e.g. *"Add narrow CPR scan configuration"*).
- Optional body explaining **what** and **why**, not **how**.
- Plain, factual language — no inflated adjectives.

### 10.6 Code Review Rules
- Every change is reviewed before merge.
- Reviews evaluate, in order: **architecture → code quality → tests →
  performance**.
- Findings reference concrete `file:line` locations.
- The build must be **warning-free** (lint, types, tests) before approval.

### 10.7 Documentation Rules
- Design changes are reflected in the relevant `docs/` file in the same PR.
- Documentation describes what the code does **now**, not discarded approaches.
- Public APIs carry docstrings; non-obvious decisions are explained near the
  code or in the design docs.

> **⚠️ Warning — Zero-warning policy.**
> A clean build is the baseline, not the goal. Warnings from any tool are fixed
> before merge, or explicitly suppressed with a justification. Unaddressed
> warnings accumulate into unmaintainable noise.

---

## 11. Non Functional Requirements

These requirements define *how well* the system must operate. They are as
binding as functional requirements.

| Attribute | Requirement |
|-----------|-------------|
| **Performance** | The scanning path is low-latency and non-blocking. Market events are processed and matches published to clients with minimal delay. Async I/O ensures concurrency without thread-per-request overhead. |
| **Scalability** | The design supports scaling from a handful of strategies to 100+ and from one data source to many, without architectural rewrites. Compute-heavy paths can be scaled horizontally. |
| **Maintainability** | Clear layering, strong typing, tests, and living documentation make the system safe to evolve. New contributors can reason about modules in isolation. |
| **Reliability** | Failures are contained. A misbehaving strategy or a flaky broker connection must not crash the platform; errors fail fast with clear, actionable context. |
| **Security** | Secrets live only in environment configuration, never in source control. Inputs are validated at the boundary. Broker credentials are handled through a dedicated security layer. |
| **Availability** | The platform runs as long-lived containerised services with health checks and automatic restart policies, supporting continuous operation during market hours. |
| **Logging** | Structured (JSON) logs are emitted to standard output for aggregation, with per-request context (method, path, status, latency). |
| **Monitoring** | Health and version endpoints expose liveness and build metadata. The design anticipates metrics and dashboards as the platform matures. |

> **💡 Tip — Reliability is a strategy-level concern.**
> Because strategies are plug-ins, the engine must isolate their failures.
> Treat a strategy raising an exception as an expected, contained event — log
> it, disable or skip it if necessary, and keep scanning.

---

## 12. Future Roadmap

The roadmap sequences the vision into deliverable versions. Details and exact
sequencing live in `12_ROADMAP.md`.

### Version 1 — Real-time Scanner (current)
The three V1 strategies, the scanner dashboard, and the full real-time
pipeline on a single data source. *See [Section 4](#4-project-scope).*

### Version 2 — Multi-broker & Paper Trading
- Multiple broker adapters running concurrently.
- A paper-trading layer that simulates execution against live signals.
- An expanded strategy library.

### Version 3 — Backtesting & Historical Data
- A historical data pipeline and a replay engine.
- Backtesting that runs the *same* strategy code used live.
- Performance analytics for strategies.

### Version 4 — Live Trading & Risk
- Real order execution through broker adapters.
- A risk-management layer (limits, guards, kill-switches).
- Position and order lifecycle management.

### Future ideas (beyond V4)
- **Strategy marketplace** — sharing, versioning, and subscribing to strategies.
- **AI-assisted analysis** — signal ranking, anomaly detection, and
  natural-language market summaries.
- **Multi-asset expansion** — options, currencies, commodities, and global
  venues.

> **⚠️ Warning — Live trading raises the stakes.**
> Versions 3 and 4 introduce financial risk and likely regulatory obligations.
> They require dedicated risk, audit, and compliance design work — not covered
> by this overview — before any implementation begins.

---

## 13. Assumptions

The project proceeds on the following assumptions. If any proves false, the
affected design must be revisited.

- **A1 — Data availability.** A broker/data source provides real-time and
  reference market data of sufficient quality and coverage for the V1
  strategies.
- **A2 — Single node is sufficient for V1.** The initial instrument universe
  and strategy count fit comfortably within a single deployment node.
- **A3 — Trusted operators.** V1 runs for a known, trusted set of users;
  multi-tenant isolation and public-facing hardening are later concerns.
- **A4 — Scanning only.** No order execution occurs in V1, so no order-routing,
  settlement, or brokerage-account risk applies yet.
- **A5 — Stable core dependencies.** The chosen frameworks (FastAPI,
  SQLAlchemy, React, etc.) remain stable at their selected major versions
  throughout V1.
- **A6 — Market-hours operation.** The system is expected to be most active
  during market hours; maintenance can be scheduled outside them.
- **A7 — Broker adapter parity.** Each broker exposes, in some form, the market
  data the internal model requires, even if the shapes differ.

---

## 14. Risks

Key risks and the mitigations the architecture provides. Full tracking lives in
the project risk register.

| # | Risk | Impact | Mitigation |
|---|------|--------|------------|
| R1 | **Broker API changes** | An upstream API change breaks data ingestion. | All broker specifics are isolated in adapters; a breaking change is contained to one adapter, not the core. Adapter contract tests catch regressions early. |
| R2 | **Network latency** | Slow upstream data delays signals and degrades the real-time experience. | Async, non-blocking I/O; timeouts and health checks; latency logged per request to detect degradation. |
| R3 | **Rate limits** | Broker throttling causes gaps or bans. | Subscription management and caching in the market engine minimise redundant calls; adapters own broker-specific throttling logic. |
| R4 | **Market data quality** | Bad ticks, gaps, or corporate-action distortions produce false signals. | Normalisation and validation at the adapter boundary; explainable signals make anomalies easier to spot and audit. |
| R5 | **Scaling** | Growth in instruments/strategies outpaces a single node. | Event-driven, stateless-where-possible design with Redis fan-out enables horizontal scaling without re-architecture. |
| R6 | **Strategy misbehaviour** | A faulty strategy consumes resources or errors repeatedly. | The engine isolates strategy execution; failures are contained, logged, and do not halt scanning. |
| R7 | **Scope creep** | Premature addition of execution/AI destabilises V1. | A hard V1 scope boundary ([Section 4](#4-project-scope)) and a versioned roadmap ([Section 12](#12-future-roadmap)). |

> **📌 Architecture callout — Isolation is the recurring mitigation.**
> Notice how many risks are mitigated by the *same* structural choice:
> confining volatility (brokers, strategies) behind stable interfaces. This is
> why the two key seams from [Section 6](#6-high-level-modules) matter so much.

---

## 15. Success Criteria

Version 1 is considered successful when **all** of the following measurable
criteria are met.

| # | Criterion | Measure |
|---|-----------|---------|
| S1 | **Strategies operational** | All three V1 strategies (Open = High, Open = Low, Narrow CPR) run continuously and produce correct, explainable matches against live data. |
| S2 | **Real-time delivery** | Scan matches are pushed to the dashboard over WebSocket within a low, consistent latency budget after the triggering market event. |
| S3 | **Dashboard usability** | The scanner dashboard displays live results in a sortable, filterable grid with charts, updating without manual refresh. |
| S4 | **Reliability** | The platform runs through a full market session without crashing; a single failing strategy or transient broker error does not take down the system. |
| S5 | **Extensibility proven** | A new strategy can be added by implementing the strategy contract alone, with no changes to the market engine or core. |
| S6 | **Operational readiness** | The full stack starts from a single command via Docker Compose; health and version endpoints report correctly; logs are structured and useful. |
| S7 | **Quality bar** | The codebase passes linting, type checking, and its automated test suite with zero warnings, and the design documents accurately describe the shipped system. |

> **💡 Tip — Success is binary per criterion, and cumulative overall.**
> Each criterion is either met or not — no partial credit. V1 ships when the
> full set is green. Track these explicitly; they are the definition of done for
> the version.

---

*End of document. This overview is maintained by the Platform Architecture team
and is updated whenever a change to the code alters the intent recorded here.*
