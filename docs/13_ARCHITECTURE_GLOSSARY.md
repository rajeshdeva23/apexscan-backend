# 13 · Architecture Glossary

> **Official Vocabulary Reference for ApexScan**
> This document is the **single source of truth for all architectural terminology** used across
> ApexScan. Every architecture document, every developer, and every AI coding assistant (Codex, Claude,
> ChatGPT, and any future tool) **must** use these definitions consistently. It is a **terminology
> reference only**: no code, no implementation, no formulas, no business logic, and no examples that
> resemble implementation. It defines *what words mean* — never *how anything is built or computed*.

---

## Document Banner

| Field | Value |
|-------|-------|
| Document | `13_ARCHITECTURE_GLOSSARY.md` |
| Title | Architecture Glossary & Canonical Vocabulary |
| Status | **Authoritative & Binding** — the canonical dictionary for all `docs/` |
| Scope | Meaning of every architectural term used in ApexScan |
| Owner | Architecture / Technical Writing |
| Governs | Terminology in `00`–`12`, `docs/adr/`, code, and AI prompts |
| Rule | Any future document that introduces a new architectural term **must update this glossary first**. |

> **The one rule that makes this document work.**
> A term means **exactly** what this glossary says it means — no more, no less — everywhere it appears.
> If a document uses a term differently, the document is wrong, not the glossary. If a term needs a new
> meaning, this glossary is updated **before** the term is used that way.

---

## Mini Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [How To Use This Glossary](#2-how-to-use-this-glossary)
3. [Core Architecture Terms](#3-core-architecture-terms)
4. [Market Data Terms](#4-market-data-terms)
5. [Market Engine Terms](#5-market-engine-terms)
6. [Strategy Engine Terms](#6-strategy-engine-terms)
7. [Backend Terms](#7-backend-terms)
8. [Database Terms](#8-database-terms)
9. [Frontend Terms](#9-frontend-terms)
10. [API Terms](#10-api-terms)
11. [Deployment Terms](#11-deployment-terms)
12. [AI Development Terms](#12-ai-development-terms)
13. [Abbreviations](#13-abbreviations)
14. [Synonyms & Deprecated Terms](#14-synonyms--deprecated-terms)
15. [Cross-Reference Matrix](#15-cross-reference-matrix)
16. [Non-Negotiable Terminology Rules](#16-non-negotiable-terminology-rules)
17. [Glossary Maintenance Checklist](#17-glossary-maintenance-checklist)
18. [Summary](#18-summary)

---

## 1. Executive Summary

### 1.1 Purpose of the Glossary

ApexScan is defined by thirteen documents, built by many hands (human and AI), and intended to last for
years. A term like **MarketContext**, **Feature**, **Score**, or **Ranking** appears dozens of times
across those documents and the codebase. If any two readers understand it differently, the architecture
fractures quietly. This glossary exists to guarantee that **one word means one thing, everywhere**.

### 1.2 Why Terminology Consistency Matters

- **Precision prevents boundary erosion.** The architecture's guarantees (facts vs decisions, features
  vs signals, transport vs authorship) live *in the words*. Blur the words and you blur the boundaries.
- **Shared vocabulary accelerates everyone.** A new contributor or AI assistant that knows these terms
  can read any document and any code with correct expectations.
- **Ambiguity is a defect.** A term used two ways is a bug waiting to happen — in a review, in a prompt,
  in a design discussion.

### 1.3 Relationship with ADRs

ADRs (`docs/adr/`) record **decisions and their rationale**. When an ADR names a concept, it uses this
glossary's definition. If an ADR establishes a new concept, that concept is **added here**. ADRs and the
glossary never disagree; where they touch, the ADR owns the *why* and the glossary owns the *meaning*.

### 1.4 Relationship with Architecture Documents

Documents `00`–`12` define **what the system is and how it is executed**. They are the primary consumers
of this vocabulary. Every term they use is defined here; every term defined here points back to the
documents that use it (§15). The glossary is the dictionary; the architecture documents are the prose.

### 1.5 Relationship with Implementation

Code and AI-generated code must **name things using these terms** (subject to each language's naming
conventions, `11` §4/§7). A class, module, event, or type that represents a glossary concept carries a
name that maps unambiguously to it. This is how the vocabulary reaches all the way down to the source.

> **Architecture Callout — the glossary is the contract between words and meaning.** Documents `00`–`12`
> agree on *what to build*; `11` agrees on *how to build it*; this document ensures everyone agrees on
> *what we are even talking about*. Without it, all the other agreement is fragile.

---

## 2. How To Use This Glossary

### 2.1 Alphabetical Organization (Within Sections)

Terms are grouped by domain (§3–§12) and ordered for readability within each group. The
**Cross-Reference Matrix** (§15) provides a flat, lookup-friendly index of every term, its defining
section, the documents that use it, and its owner.

### 2.2 Canonical Definitions

Each term has **exactly one canonical definition** here. That definition is authoritative. No document
may redefine a term locally; if a document needs a narrower sense, it references this definition and
qualifies it in context — it does not overwrite it.

### 2.3 Aliases

Some concepts have a common informal alias. Where an alias is acceptable, it is noted. Where an alias is
**forbidden** because it causes ambiguity, it appears in the **Synonyms & Deprecated Terms** table (§14)
with the preferred term and the reason.

### 2.4 Deprecated Terms

A term is **deprecated** when it has been replaced by a clearer canonical term. Deprecated terms are
listed in §14 and must **not** appear in new documents, code, or prompts. Existing occurrences are
migrated to the canonical term when touched (`11` §14.5).

### 2.5 Cross-References

Definitions link to related terms and to the documents that use them (§3–§12 and §15). Follow
cross-references to understand a term in the context of its neighbors — meaning often lives at the
boundary between two terms (e.g., **Feature** vs **Signal**).

### 2.6 Versioning Philosophy

- The glossary is **versioned with the documentation set**. A change to a definition is a change to the
  architecture's shared meaning and is treated with the same seriousness as a change to a contract.
- Definitions evolve **additively** where possible: clarify rather than reinterpret. A genuine change of
  meaning is recorded (old term → §14) so history is never silently rewritten.
- **New term first, usage second:** a new architectural term is added here **before** it is used anywhere
  else (the governing rule in the banner).

> **Note.** When reading any ApexScan document, treat an unfamiliar capitalized term (e.g.,
> **MarketContext**, **StrategyResult**) as a defined term and look it up here rather than inferring its
> meaning from everyday English.

---

## 3. Core Architecture Terms

*Each term below carries: **Definition**, **Purpose**, **Owner**, **Related Terms**, and **Used In**.*

**Architecture**
- **Definition:** The frozen, documented design of ApexScan — its layers, boundaries, contracts, and
  decisions — as captured in documents `00`–`12` and the ADRs.
- **Purpose:** The stable structure every contribution must respect.
- **Owner:** Architecture.
- **Related:** Layer, Clean Architecture, Architecture Freeze.
- **Used In:** `00`, `01`, all.

**Layer**
- **Definition:** A horizontal division of the system with a single responsibility and a defined
  dependency direction (e.g., API, service, repository, infrastructure).
- **Purpose:** Separation of concerns and enforceable dependency direction.
- **Owner:** Architecture.
- **Related:** Layered Architecture, Dependency Injection, Module.
- **Used In:** `01`, `03`, `11`.

**Module**
- **Definition:** A cohesive unit of code with one documented responsibility and a clear public surface.
- **Purpose:** The unit of ownership and isolation.
- **Owner:** The owning layer/component.
- **Related:** Component, Service, Layer.
- **Used In:** `03`, `11`.

**Component**
- **Definition:** A named architectural building block that performs a distinct role (e.g., the Market
  Engine, the WebSocket Manager).
- **Purpose:** The vocabulary for the system's major parts.
- **Owner:** Architecture.
- **Related:** Module, Service, Market Engine, Strategy Engine.
- **Used In:** `01`, `03`, `06`, `07`, `09`.

**Service**
- **Definition:** A backend unit in the service layer that orchestrates a business operation and enforces
  invariants; contains business logic, no transport or persistence detail.
- **Purpose:** Where business decisions are made.
- **Owner:** Service Layer.
- **Related:** Service Layer, Repository, Business logic.
- **Used In:** `03`, `08`, `11`.

**Repository**
- **Definition:** The sole abstraction through which persistent data is accessed; exposes
  intention-revealing operations and contains **no business logic**.
- **Purpose:** Encapsulate persistence; keep storage detail out of services.
- **Owner:** Repository Layer.
- **Related:** Repository Pattern, Persistence Layer, Entity, Transaction.
- **Used In:** `02`, `03`, `08`, `11`.

**Adapter**
- **Definition:** A component that translates between an external system's shape and ApexScan's internal
  contract.
- **Purpose:** Isolate the system from external specifics.
- **Owner:** The layer owning the boundary (e.g., Data Provider).
- **Related:** Broker Adapter, Provider, Data Provider.
- **Used In:** `05`, `03`.

**Broker Adapter**
- **Definition:** The specific Adapter that implements the broker abstraction for one broker, translating
  its feed into normalized market data.
- **Purpose:** Make the system broker-agnostic above the Data Provider layer.
- **Owner:** Data Provider Layer.
- **Related:** Adapter, Data Provider, Provider, Market Data.
- **Used In:** `05`.

**Provider**
- **Definition:** A source of external data or capability accessed through an abstraction (a broker is a
  provider of market data).
- **Purpose:** Generalize external sources behind contracts.
- **Owner:** Data Provider Layer.
- **Related:** Data Provider, Broker Adapter.
- **Used In:** `05`.

**Data Provider**
- **Definition:** The layer (a.k.a. Broker Abstraction Layer) that owns all Broker Adapters and produces
  normalized market data; the Market Engine consumes it without knowing the broker.
- **Purpose:** The single seam between ApexScan and any broker.
- **Owner:** Data Provider Layer.
- **Related:** Broker Adapter, Normalization, Market Data.
- **Used In:** `05`, `01`, `03`.

**Strategy**
- **Definition:** A plug-in that interprets **Features** from a MarketContext into a **StrategyResult**;
  it never measures the market and never accesses brokers.
- **Purpose:** The unit of market interpretation.
- **Owner:** Strategy Engine.
- **Related:** Strategy Engine, StrategyResult, Feature, Plugin.
- **Used In:** `07`.

**Strategy Manager**
- **Definition:** The component that owns strategy registration, lifecycle, and execution orchestration
  within the Strategy Engine.
- **Purpose:** Manage the population of strategies without any strategy editing the engine.
- **Owner:** Strategy Engine.
- **Related:** Strategy, Strategy Lifecycle, Plugin Registry.
- **Used In:** `07`.

**Market Engine**
- **Definition:** The component that transforms normalized market data into standardized, immutable,
  versioned market **facts** (MarketContext); it computes facts, never decisions.
- **Purpose:** The system's trust anchor for market intelligence.
- **Owner:** Market Engine.
- **Related:** MarketContext, Feature, Data Provider, Strategy Engine.
- **Used In:** `06`, `01`, `07`.

**Backend**
- **Definition:** The server-side of ApexScan (FastAPI application, services, repositories, engines,
  event bus, WebSocket) as specified in `03`.
- **Purpose:** Where facts and results are produced and served.
- **Owner:** Backend.
- **Related:** Frontend, API, WebSocket, Layer.
- **Used In:** `03`, all.

**Frontend**
- **Definition:** The client-side React application that renders the platform's truth and expresses
  subscription intent; it never computes or re-ranks.
- **Purpose:** Present authoritative data to the user.
- **Owner:** Frontend.
- **Related:** Backend, API, WebSocket, Component.
- **Used In:** `04`, `09`.

**API**
- **Definition:** The versioned, resource-oriented HTTP contract through which clients read state and
  submit intent; defined in `08`.
- **Purpose:** The stable synchronous interface.
- **Owner:** API / Interface Layer.
- **Related:** REST, Endpoint, Version, WebSocket.
- **Used In:** `08`, `03`, `04`.

**WebSocket**
- **Definition:** The persistent, push-based transport that delivers live change from backend to
  frontend; defined in `09`. It transports, never computes.
- **Purpose:** Real-time delivery of facts, results, and rankings.
- **Owner:** Delivery Layer (WebSocket Manager).
- **Related:** Event Bus, API, Frontend, Session.
- **Used In:** `09`, `04`.

**Event Bus**
- **Definition:** The decoupled, typed, fire-and-forget delivery mechanism between backend components;
  producers publish, consumers subscribe.
- **Purpose:** Decouple producers from consumers (a contract, not a technology).
- **Owner:** Backend.
- **Related:** Event-Driven Architecture, Event Publication, WebSocket.
- **Used In:** `01`, `03`, `09`.

**Worker**
- **Definition:** A backend process/unit that runs work concurrently or in the background (planned
  `workers/`); observable and gracefully shut down.
- **Purpose:** Execute concurrent/background work off the request path.
- **Owner:** Backend.
- **Related:** Background Job, Scheduler, Async First.
- **Used In:** `03`, `10`.

**Cache**
- **Definition:** A fast, non-authoritative store (Redis) for hot, expensive-to-derive data; never the
  source of truth.
- **Purpose:** Reduce latency and load without owning truth.
- **Owner:** Backend / Infrastructure.
- **Related:** Redis Cache, TTL, Source of Truth.
- **Used In:** `03`, `08`, `10`.

**Repository Pattern**
- **Definition:** The design pattern of accessing persistence exclusively through Repositories.
- **Purpose:** Keep storage concerns isolated and business logic out of persistence.
- **Owner:** Architecture.
- **Related:** Repository, Persistence Layer, Service.
- **Used In:** `03`, `11`.

**Dependency Injection**
- **Definition:** Supplying a component's dependencies from outside rather than constructing them
  internally.
- **Purpose:** Testability, explicit wiring, decoupling.
- **Owner:** Architecture.
- **Related:** Layer, Service, Clean Architecture.
- **Used In:** `03`, `08`, `11`.

**Clean Architecture**
- **Definition:** The architectural style in which dependencies point inward toward the domain and outer
  layers depend on inner ones, never the reverse.
- **Purpose:** Protect the core from external change.
- **Owner:** Architecture.
- **Related:** Layered Architecture, Dependency Injection, Layer.
- **Used In:** `01`, `03`, `11`.

**Layered Architecture**
- **Definition:** The organization of the system into horizontal layers with a defined dependency
  direction (the Dependency Rule).
- **Purpose:** Separation of concerns; enforceable boundaries.
- **Owner:** Architecture.
- **Related:** Clean Architecture, Layer, Dependency Injection.
- **Used In:** `01`, `03`, `11`.

**Event-Driven Architecture**
- **Definition:** The style in which components communicate by publishing and subscribing to typed events
  rather than calling each other directly.
- **Purpose:** Decoupling, extensibility, and real-time flow.
- **Owner:** Architecture.
- **Related:** Event Bus, Event Publication, WebSocket.
- **Used In:** `01`, `09`, `03`.

**Async First**
- **Definition:** The principle that all I/O-bound backend work is asynchronous; blocking calls never run
  on the event loop.
- **Purpose:** Keep the real-time system responsive under concurrency.
- **Owner:** Backend.
- **Related:** Worker, Event-Driven Architecture.
- **Used In:** `03`, `11`.

**Configuration**
- **Definition:** Externalized settings (and secrets) that control runtime behaviour, accessed only
  through the settings abstraction and validated at startup.
- **Purpose:** One set of images behaving correctly per environment.
- **Owner:** Backend / Platform.
- **Related:** Environment, Source of Truth, Deployment.
- **Used In:** `03`, `10`, `11`.

**Environment**
- **Definition:** A named runtime context (development, testing, staging, production) — the same system
  under different configuration and scale.
- **Purpose:** Isolated contexts with a single system definition.
- **Owner:** Platform / DevOps.
- **Related:** Configuration, Deployment.
- **Used In:** `10`.

**Session** *(connection sense)*
- **Definition:** The ephemeral logical envelope around a client connection (identity, subscriptions,
  health); distinct from a **Trading Session** (§4).
- **Purpose:** Track a live client's state for the life of its connection.
- **Owner:** WebSocket Manager.
- **Related:** WebSocket, Subscription, Trading Session.
- **Used In:** `09`.

**Context**
- **Definition:** A bounded snapshot of relevant state at a point in the pipeline; the canonical instance
  is the **MarketContext** (§5). "Context" unqualified is ambiguous and should be qualified.
- **Purpose:** Name the immutable state passed between stages.
- **Owner:** Depends on the qualifier (Market Engine for MarketContext).
- **Related:** MarketContext, Session Context, Immutable Context.
- **Used In:** `06`, `07`.

> ⚠️ **"Context" and "Session" are overloaded English words.** In ApexScan they are always qualified:
> **MarketContext** (market facts) and **Session Context** / **Trading Session** (market timing) are
> different from a WebSocket **Session** (a client connection). Never use them bare where meaning matters.

---

## 4. Market Data Terms

*Definitions describe **what a term means** — never how any value is computed. No formulas.*

| Term | Definition | Owner / Used In |
|------|------------|-----------------|
| **Market Data** | Raw or normalized information about instruments and their trading, sourced from a broker. | Data Provider / `05`, `06` |
| **Tick** | A single discrete market data update event for an instrument. | Data Provider / `05`, `09` |
| **Quote** | A point-in-time statement of an instrument's current bid/ask (and related fields). | Data Provider / `05` |
| **Candle** | A summarized market data unit covering one time interval (see OHLC). | Data Provider / `06` |
| **OHLC** | The Open, High, Low, and Close values summarizing an interval; a descriptor, not a computation. | Data Provider / `06` |
| **Volume** | The traded quantity over a period or event. | Data Provider / `06` |
| **Order Book** | The set of resting buy/sell interest for an instrument at various price levels. | Data Provider / `05` |
| **Market Depth** | The view of the Order Book across multiple price levels. | Data Provider / `05` |
| **Bid** | The buy-side interest side of a Quote/Order Book. | Data Provider / `05` |
| **Ask** | The sell-side interest side of a Quote/Order Book (a.k.a. offer). | Data Provider / `05` |
| **Spread** | The relationship between Bid and Ask as a named concept (not a formula here). | Data Provider / `06` |
| **Liquidity** | A qualitative concept describing how readily an instrument trades; named, not measured here. | Market Engine / `06` |
| **Instrument** | A tradable entity (the abstract thing a Symbol identifies). | Data Provider / `05`, `06` |
| **Symbol** | The identifier for an Instrument on an Exchange. | Data Provider / `05` |
| **Exchange** | The venue/market on which Instruments trade. | Data Provider / `05` |
| **Expiry** | The expiration attribute of a dated Instrument (e.g., a derivative). | Data Provider / `05` |
| **Strike** | The strike attribute of an options Instrument. | Data Provider / `05` |
| **Lot Size** | The tradable unit size attribute of an Instrument. | Data Provider / `05` |
| **Previous Day Data** | Market Data attributed to the prior trading day, used as reference. | Market Engine / `06` |
| **Historical Data** | Market Data from the past, retrieved as a snapshot (not the live stream). | Market Engine / `06`, `08` |
| **Live Data** | Market Data arriving in real time via the current feed and stream. | Data Provider / `05`, `09` |
| **Market State** | A named, standardized description of current market conditions as a **fact** (never a decision). | Market Engine / `06` |
| **Trading Session** | A defined market timing period (e.g., open/close boundaries); distinct from a connection Session (§3). | Market Engine / `06` |
| **Session Context** | The bounded market-timing context in which facts are interpreted; a qualifier of Context. | Market Engine / `06` |
| **Opening Session** | The Trading Session period around market open, as a named reference. | Market Engine / `06` |
| **Closing Session** | The Trading Session period around market close, as a named reference. | Market Engine / `06` |
| **Gap** | A named relationship between reference periods (e.g., prior close vs current open); concept only. | Market Engine / `06` |
| **Opening Range** | A named reference range associated with the opening period; concept only, no computation. | Market Engine / `06` |
| **Rolling Window** | A moving span over recent data used as a basis for a Derived Feature; concept only. | Market Engine / `06` |

> **Note.** Terms like Spread, Gap, Opening Range, and Rolling Window are defined here as **named
> concepts** only. How (or whether) any value is derived from them is a Feature concern owned by the
> Market Engine (`06`) and is deliberately **not** described in this glossary (no formulas).

---

## 5. Market Engine Terms

| Term | Definition | Owner / Used In |
|------|------------|-----------------|
| **MarketContext** | The immutable, versioned snapshot of standardized market **facts** produced by the Market Engine. The canonical unit of market intelligence. | Market Engine / `06`, `07`, `09` |
| **Feature** | A standardized, named market **fact** carried by a MarketContext. A Feature is **never** a trading signal or a decision. | Market Engine / `06` |
| **Derived Feature** | A Feature computed from other data/Features by the Market Engine; still a fact, never a signal. | Market Engine / `06` |
| **Feature Registry** | The catalog of known Features, their identities, and versions. | Market Engine / `06` |
| **Feature Version** | The version stamped on a Feature/MarketContext enabling ordering and idempotency. | Market Engine / `06`, `09` |
| **Feature Dependency** | A declared dependency of one Feature on another for ordered computation. | Market Engine / `06` |
| **Historical Context** | The historical portion of market intelligence available to the engine as reference facts. | Market Engine / `06` |
| **Validation Pipeline** | The stage that checks incoming/normalized data for well-formedness before facts are produced. | Market Engine / `06` |
| **Normalization** | The transformation of broker-specific data into the standard internal shape (owned at the Data Provider boundary, consumed by the engine). | Data Provider / `05`, `06` |
| **Session Statistics** | Standardized facts summarizing a Trading Session; facts, not decisions. | Market Engine / `06` |
| **Previous Day Landmark** | A named reference point derived from Previous Day Data. | Market Engine / `06` |
| **Event Publication** | The act of a component emitting a typed event onto the Event Bus. | Backend / `06`, `09`, `03` |
| **Context Snapshot** | An immutable point-in-time capture of a MarketContext. | Market Engine / `06` |
| **Immutable Context** | The property that a MarketContext is never mutated after creation; a new version is produced instead. | Market Engine / `06`, `09` |

> ⚠️ **Feature ≠ Signal.** A Feature is a *measured fact* about the market. A trading signal is a
> *decision/interpretation*, which is the Strategy Engine's domain. Using "Feature" to mean "signal" is a
> terminology violation (§16).

---

## 6. Strategy Engine Terms

| Term | Definition | Owner / Used In |
|------|------------|-----------------|
| **Strategy** | A plug-in that interprets Features into a StrategyResult; never measures the market, never accesses brokers. | Strategy Engine / `07` |
| **Strategy Category** | A classification grouping strategies by kind (metadata, not behaviour). | Strategy Engine / `07` |
| **Strategy Configuration** | The externalized settings for a strategy; never affects an in-flight evaluation. | Strategy Engine / `07` |
| **Strategy Registration** | The act of making a Strategy known to the Strategy Manager via the plug-in contract. | Strategy Engine / `07` |
| **Strategy Lifecycle** | The defined states a strategy moves through (registered, enabled, disabled, faulted). | Strategy Engine / `07` |
| **StrategyResult** | The immutable output of a single Strategy evaluation. The canonical unit of interpretation. | Strategy Engine / `07`, `08`, `09` |
| **Score** | A standardized numeric representation within a StrategyResult; its **meaning is strategy-owned**. Distinct from Confidence. | Strategy Engine / `07` |
| **Confidence** | A separate standardized measure of certainty associated with a result; **never** a synonym for Score. | Strategy Engine / `07` |
| **Ranking** | The authoritative ordering of results for a scanner, owned by the Strategy Engine; **never re-sorted** downstream. | Strategy Engine / `07`, `08`, `09` |
| **Ranking Engine** | The component that produces a Ranking from results; it orders, it never re-computes Score. | Strategy Engine / `07` |
| **Evaluation** | A single execution of a Strategy over a MarketContext producing a StrategyResult. | Strategy Engine / `07` |
| **Dependency** *(strategy sense)* | A declared prerequisite a Strategy needs (e.g., specific Features) to evaluate. | Strategy Engine / `07` |
| **Capability** | A declared thing a Strategy can do or requires; metadata used by the manager. | Strategy Engine / `07` |
| **Metadata** *(strategy sense)* | Descriptive, non-behavioural information about a Strategy (name, category, capabilities). | Strategy Engine / `07` |
| **Diagnostics** | Observability information about a Strategy's execution and health; not part of its result meaning. | Strategy Engine / `07` |
| **Plugin** | A unit (a Strategy) added to the system through a defined contract without modifying the engine. | Strategy Engine / `07` |
| **Plugin Registry** | The catalog of registered plug-ins known to the Strategy Manager. | Strategy Engine / `07` |
| **Plugin Discovery** | The mechanism by which available plug-ins are found and registered. | Strategy Engine / `07` |

> ⚠️ **Score ≠ Confidence, and Ranking never changes Score.** These three are distinct by definition.
> Ranking orders results; it must not alter the Score or re-interpret a StrategyResult (§16).

---

## 7. Backend Terms

| Term | Definition | Owner / Used In |
|------|------------|-----------------|
| **Application Layer** | The outermost backend layer that wires and exposes the application (e.g., the app factory, routing). | Backend / `03` |
| **Domain Layer** | The innermost layer holding core business concepts and rules, independent of frameworks. | Backend / `03` |
| **Infrastructure Layer** | The layer providing technical capabilities (DB, cache, external clients) to inner layers. | Backend / `03` |
| **Persistence Layer** | The part of infrastructure responsible for storing/retrieving data (accessed via Repositories). | Backend / `02`, `03` |
| **Service Layer** | The layer that orchestrates business operations and enforces invariants. | Backend / `03`, `08` |
| **Repository Layer** | The layer of Repositories mediating all persistence access. | Backend / `02`, `03` |
| **Middleware** | A cross-cutting request/response processing stage (e.g., logging) in the application layer. | Backend / `03` |
| **Dependency Graph** | The directed structure of module dependencies; must be acyclic and inward-pointing. | Backend / `03`, `11` |
| **Validation** | The act of checking input for well-formedness at a boundary before business logic. | Backend / `03`, `08` |
| **Exception** | A raised signal of an exceptional condition; never swallowed silently. | Backend / `03`, `11` |
| **Health Check** | A machine-readable report of a component's operational state. | Backend / `03`, `10` |
| **Readiness** | The check indicating an instance can accept traffic **now**; failing removes it from traffic. | Backend / `03`, `10` |
| **Liveness** | The check indicating an instance is healthy; failing triggers a restart. | Backend / `03`, `10` |
| **Worker** | A concurrent/background execution unit (see §3). | Backend / `03`, `10` |
| **Scheduler** | A component that triggers work on a schedule or cadence (planned). | Backend / `03` |
| **Background Job** | A unit of work executed off the request path, observably and gracefully. | Backend / `03`, `10` |

---

## 8. Database Terms

| Term | Definition | Owner / Used In |
|------|------------|-----------------|
| **Entity** | A persistent domain object with a distinct identity, accessed via a Repository. | Database / `02`, `03` |
| **Aggregate** | A cluster of related Entities treated as a single consistency boundary. | Database / `02` |
| **Source of Truth** | The authoritative store for a piece of state. In ApexScan, PostgreSQL is the source of truth (ADR-001); Cache never is. | Database / `02`, ADR-001 |
| **Transaction** | An atomic unit of database work that fully commits or fully rolls back. | Database / `02`, `11` |
| **Index** | A structure that speeds lookups on chosen fields; a performance concept, not business logic. | Database / `02` |
| **Primary Key** | The field(s) uniquely identifying an Entity. | Database / `02` |
| **Foreign Key** | A field referencing another Entity's Primary Key, expressing a relationship. | Database / `02` |
| **JSONB** | The binary JSON storage type used for flexible, semi-structured fields per the JSONB usage policy. | Database / `02` |
| **Redis Cache** | The Redis-backed Cache used for hot data and pub/sub; non-authoritative. | Infrastructure / `03`, `10` |
| **Memory Cache** | An in-process cache for very short-lived, local reuse; non-authoritative. | Backend / `03` |
| **TTL** | Time-To-Live: the bounded lifetime of a cached value before expiry. | Infrastructure / `03`, `10` |
| **Retention** | The policy governing how long data/logs/backups are kept. | Platform / `10` |
| **Partition** | A division of a large dataset for manageability/performance (concept). | Database / `02` |
| **Migration** | A versioned, reviewed change to the database schema; never hand-applied to a running database. | Database / `02`, `10`, `11` |

---

## 9. Frontend Terms

| Term | Definition | Owner / Used In |
|------|------------|-----------------|
| **Page** | A route-level frontend unit composing a full view. | Frontend / `04` |
| **Layout** | A structural frontend unit owning shared chrome around pages. | Frontend / `04` |
| **Component** *(frontend sense)* | A reusable UI unit rendered from props; one per file. | Frontend / `04`, `11` |
| **Container Component** | A component owning data/behaviour concerns (fetching, state) and wiring presentational components. | Frontend / `04`, `11` |
| **Presentational Component** | A component that renders purely from props with no data-fetching or business concern. | Frontend / `04`, `11` |
| **Hook** | A composable unit of stateful frontend logic following the rules of hooks. | Frontend / `04`, `11` |
| **Global State** | Client state shared app-wide via the client store (Zustand). A subset of UI/client state. | Frontend / `04` |
| **Local State** | Client state owned by a single component. | Frontend / `04` |
| **Server State** | Data owned by the backend and fetched/cached client-side (TanStack Query); never hand-copied into client state. | Frontend / `04`, `11` |
| **UI State** | Client-owned presentation state (selections, toggles), distinct from Server State. | Frontend / `04` |
| **Grid** | The data-grid presentation surface for tabular results (AG Grid). | Frontend / `04` |
| **Chart** | The charting presentation surface for time-series/market visualization. | Frontend / `04` |
| **Dashboard** | A composed Page presenting multiple coordinated views. | Frontend / `04` |
| **Widget** | A self-contained presentational unit within a Dashboard. | Frontend / `04` |

---

## 10. API Terms

| Term | Definition | Owner / Used In |
|------|------------|-----------------|
| **REST** | The resource-oriented, HTTP-native style of the synchronous API. | API / `08` |
| **Endpoint** | A specific, versioned, addressable operation on a Resource. | API / `08` |
| **Version** *(API sense)* | The explicit major version of the API contract governing its compatibility promise. | API / `08` |
| **Resource** | A noun-oriented thing the API exposes (the unit the contract is organized around). | API / `08` |
| **Request** | A client's inbound call to an Endpoint. | API / `08` |
| **Response** | The API's outbound answer, conforming to the uniform shape/error model. | API / `08` |
| **Idempotency** | The property that repeating an operation yields the same end state; enables safe retries. | API / `08`, `11` |
| **Pagination** | The mechanism for returning bounded slices of a collection. | API / `08` |
| **Filtering** | Narrowing a collection along declared dimensions (never client-supplied logic). | API / `08` |
| **Sorting** | Ordering a collection along declared dimensions; preserves authoritative order where one exists. | API / `08` |
| **Authentication** | Establishing **who** is calling (reserved future seam in Phase 1). | API / `08`, `09` |
| **Authorization** | Determining **what** a caller may do (reserved future seam; enforced before the service). | API / `08` |
| **Rate Limiting** | Bounding request volume per client for availability and fairness (reserved edge capability). | API / `08`, `10` |

---

## 11. Deployment Terms

| Term | Definition | Owner / Used In |
|------|------------|-----------------|
| **Container** | The unit of packaging and deployment; carries its dependencies for reproducibility. | Platform / `10` |
| **Docker** | The container runtime/toolchain used to build and run Containers. | Platform / `10` |
| **Environment** *(deploy sense)* | See §3 — a named runtime context differing only by configuration/scale. | Platform / `10` |
| **Reverse Proxy** | The single public entry point that terminates TLS and routes traffic internally. | Platform / `10` |
| **Nginx** | The reverse proxy technology used in Phase 1 production. | Platform / `10` |
| **SSL** | The certificate/encryption mechanism securing transport (colloquially TLS). | Platform / `10` |
| **HTTPS** | HTTP over TLS; the only permitted external transport. | Platform / `10` |
| **Health Check** *(deploy sense)* | See §7 — consumed by proxy/monitors/automation to act on true state. | Platform / `10` |
| **Monitoring** | The collection of metrics/alerts turning signals into awareness. | SRE / `10` |
| **Logging** | The structured, scrubbed, retained record of what the system did. | Platform / `10`, `11` |
| **Scaling** | Adjusting capacity vertically (bigger) or horizontally (more instances). | SRE / `10` |
| **Rollback** | Re-activating the previous immutable artifact to undo a deploy. | Platform / `10` |
| **Deployment** | The act/result of promoting an immutable artifact to an Environment. | Platform / `10` |

---

## 12. AI Development Terms

| Term | Definition | Owner / Used In |
|------|------------|-----------------|
| **Architecture Freeze** | The state in which the architecture (`00`–`11`) is fixed and changes only via ADR/RFC. | Architecture / `12`, `11` |
| **ADR** | Architecture Decision Record: a recorded, binding decision with its rationale (`docs/adr/`). | Architecture / `docs/adr/`, `11` |
| **RFC** | Request For Comments: a proposal for a significant change, discussed before implementation. | Architecture / `11` |
| **Codex** | An AI coding assistant; bound by the AI Development Guidelines (`11` §17). | Engineering / `11` |
| **Claude** | An AI coding assistant; bound by the AI Development Guidelines (`11` §17). | Engineering / `11` |
| **AI Assistant** | Any AI tool contributing to ApexScan; held to `11` and this glossary. | Engineering / `11` |
| **Implementation Contract** | The set of contracts (API `08`, events `09`, data `02`) and standards (`11`) that implementation must honour. | Engineering / `08`, `09`, `11` |
| **Engineering Standards** | The binding rules for how code is written, in `11_CODING_GUIDELINES.md`. | Engineering / `11` |
| **Definition of Done** | The explicit, per-phase criteria that must be met for work to count as complete (`12`). | Delivery / `12` |
| **Acceptance Criteria** | The verifiable conditions confirming a phase/feature satisfies its intent (`12`). | Delivery / `12` |

> **Architecture Callout — AI assistants share this vocabulary.** Because Codex, Claude, and ChatGPT are
> bound by `11` §17 and this glossary, a correctly-termed prompt yields correctly-termed code. Consistent
> vocabulary is what lets human and AI contributors understand each other precisely.

---

## 13. Abbreviations

> **Note.** Indicator abbreviations (CPR, VWAP, ATR, EMA, SMA, MACD, RSI) are listed **only** so their
> letters are understood when encountered. ApexScan Phase 1 implements **no** indicator logic, and this
> glossary contains **no formulas or trading rules** for any of them — they are out-of-scope names, not
> specifications.

| Abbreviation | Expansion | Notes |
|--------------|-----------|-------|
| **API** | Application Programming Interface | See §10. |
| **ADR** | Architecture Decision Record | See §12; binding. |
| **RFC** | Request For Comments | See §12. |
| **DI** | Dependency Injection | See §3. |
| **OHLC** | Open, High, Low, Close | Market data descriptor (§4); no formula. |
| **CPR** | Central Pivot Range | Indicator name only — **out of scope**, no formula/logic in ApexScan. |
| **VWAP** | Volume-Weighted Average Price | Indicator name only — **out of scope**, no formula/logic. |
| **ATR** | Average True Range | Indicator name only — **out of scope**, no formula/logic. |
| **EMA** | Exponential Moving Average | Indicator name only — **out of scope**, no formula/logic. |
| **SMA** | Simple Moving Average | Indicator name only — **out of scope**, no formula/logic. |
| **MACD** | Moving Average Convergence Divergence | Indicator name only — **out of scope**, no formula/logic. |
| **RSI** | Relative Strength Index | Indicator name only — **out of scope**, no formula/logic. |
| **TTL** | Time-To-Live | Cache expiry (§8). |
| **JWT** | JSON Web Token | Future authentication mechanism (§10, §12). |
| **REST** | Representational State Transfer | API style (§10). |
| **JSON** | JavaScript Object Notation | Data interchange format. |
| **JSONB** | JSON Binary (PostgreSQL type) | Storage type (§8). |
| **SQL** | Structured Query Language | Database query language. |
| **UI** | User Interface | Frontend presentation (§9). |
| **UX** | User Experience | The overall experience of using the product. |
| **CI** | Continuous Integration | Automated build/verify on every change. |
| **CD** | Continuous Delivery/Deployment | Automated promotion/release. |
| **WS** | WebSocket | Real-time transport (§3, `09`). |
| **RBAC** | Role-Based Access Control | Future authorization model (§10, `08`). |
| **RPO** | Recovery Point Objective | Max acceptable data loss (`10`). |
| **RTO** | Recovery Time Objective | Max acceptable restore time (`10`). |
| **SPOF** | Single Point Of Failure | Phase 1 single-node production (`10`). |
| **DoD** | Definition of Done | Per-phase completion criteria (`12`). |
| **TLS** | Transport Layer Security | Transport encryption (a.k.a. SSL, §11). |
| **CORS** | Cross-Origin Resource Sharing | Origin allow-listing (§10, §11). |

---

## 14. Synonyms & Deprecated Terms

Deprecated terms must **not** appear in new documents, code, or AI prompts. Existing occurrences are
migrated to the preferred term when touched (`11` §14.5).

| Preferred Term | Deprecated / Avoided Term | Reason |
|----------------|---------------------------|--------|
| **MarketContext** | Market Snapshot, Market State Object | Canonical terminology; "snapshot" is a property, not the object. |
| **StrategyResult** | Signal Result, Strategy Output, Signal | Avoid ambiguity; "signal" implies a trading decision the result is not. |
| **Feature** | Indicator, Signal, Metric | A Feature is a fact; "signal"/"indicator" imply interpretation/trading logic. |
| **Score** | Rank Value, Rating, Confidence | Score and Confidence are distinct (§6); "rank value" confuses Score with Ranking. |
| **Confidence** | Certainty Score, Probability | Distinct from Score; avoid implying a specific statistical meaning. |
| **Ranking** | Sort, Ordering Score, Leaderboard | Ranking is authoritative ordering; it is not a Score and not a client-side sort. |
| **Data Provider** | Broker Layer, Feed Handler | Canonical name for the broker abstraction layer (`05`). |
| **Broker Adapter** | Broker Client, Connector | Canonical name for the per-broker Adapter (`05`). |
| **Market Engine** | Indicator Engine, Signal Engine | It computes facts, not signals/indicators. |
| **Strategy** | Scanner Rule, Algo, Signal Generator | Canonical plug-in term; avoids implying embedded trading rules. |
| **Event Bus** | Message Queue, PubSub Layer | Canonical name; it is a contract, not a specific technology. |
| **Source of Truth** | Master DB, Primary Store | Canonical term; ties to ADR-001. |
| **Cache** | Store, Temp DB | A Cache is explicitly non-authoritative. |
| **WebSocket (WS)** | Socket, Live Channel, Push API | Canonical transport name (`09`). |
| **Trading Session** | Market Session (when timing is meant) | Disambiguate from a connection **Session** (§3). |
| **Session** *(connection)* | User Session, Socket Session | The ephemeral connection envelope (§3), not a trading period. |
| **Container** | Image (when the running unit is meant) | An Image is the artifact; a Container is the running instance. |
| **Repository** | DAO, Data Layer Object | Canonical persistence-access term. |

> ⚠️ **The most dangerous synonyms are "signal", "indicator", and "snapshot".** Each quietly imports a
> meaning ApexScan deliberately rejects. Use **Feature**, **StrategyResult**, and **MarketContext**.

---

## 15. Cross-Reference Matrix

A flat index of major terms for quick lookup. (Every term defined in §3–§12 is governed by this matrix;
representative high-traffic terms are listed here.)

| Term | Definition Section | Referenced Documents | Primary Owner |
|------|-------------------|----------------------|---------------|
| Architecture | §3 | `00`–`12`, ADRs | Architecture |
| Layer / Layered Architecture | §3 | `01`, `03`, `11` | Architecture |
| Clean Architecture | §3 | `01`, `03`, `11` | Architecture |
| Dependency Injection | §3 | `03`, `08`, `11` | Architecture |
| Event Bus / Event-Driven Architecture | §3 | `01`, `03`, `09` | Backend |
| Data Provider | §3 | `05`, `01`, `03` | Data Provider Layer |
| Broker Adapter | §3 | `05` | Data Provider Layer |
| Market Engine | §3 | `06`, `01`, `07` | Market Engine |
| MarketContext | §5 | `06`, `07`, `09` | Market Engine |
| Feature / Derived Feature | §5 | `06` | Market Engine |
| Feature Version | §5 | `06`, `09` | Market Engine |
| Strategy | §3/§6 | `07` | Strategy Engine |
| Strategy Manager | §3 | `07` | Strategy Engine |
| StrategyResult | §6 | `07`, `08`, `09` | Strategy Engine |
| Score / Confidence | §6 | `07` | Strategy Engine |
| Ranking / Ranking Engine | §6 | `07`, `08`, `09` | Strategy Engine |
| Plugin / Plugin Registry | §6 | `07` | Strategy Engine |
| Service / Service Layer | §3/§7 | `03`, `08`, `11` | Service Layer |
| Repository / Repository Pattern | §3/§7 | `02`, `03`, `08`, `11` | Repository Layer |
| Source of Truth | §8 | `02`, ADR-001 | Database |
| JSONB | §8 | `02` | Database |
| Migration | §8 | `02`, `10`, `11` | Database |
| API / REST / Endpoint | §3/§10 | `08`, `03`, `04` | API Layer |
| Version (API) | §10 | `08` | API Layer |
| Idempotency | §10 | `08`, `11` | API Layer |
| WebSocket (WS) | §3 | `09`, `04` | Delivery Layer |
| Session (connection) | §3 | `09` | WebSocket Manager |
| Server State / UI State | §9 | `04`, `11` | Frontend |
| Container / Deployment | §11 | `10` | Platform |
| Rollback | §11 | `10` | Platform |
| Health Check / Readiness / Liveness | §7 | `03`, `10` | Backend / SRE |
| ADR / RFC | §12 | `docs/adr/`, `11`, `12` | Architecture |
| Definition of Done / Acceptance Criteria | §12 | `12` | Delivery |
| Architecture Freeze | §12 | `12`, `11` | Architecture |

---

## 16. Non-Negotiable Terminology Rules

These rules are **binding**. Using a term against its definition is a defect caught in review (`11` §18).

### Market Engine & Facts
| # | Rule |
|---|------|
| 1 | **MarketContext** always means the immutable, versioned object of market facts produced by the Market Engine. |
| 2 | A **MarketContext** is never mutated; a new versioned instance is produced instead. |
| 3 | **Feature** always means a standardized market **fact**, never a trading signal or decision. |
| 4 | **Feature** is never used as a synonym for indicator, metric, or signal. |
| 5 | A **Derived Feature** is still a fact; deriving it never makes it a decision. |
| 6 | **Feature Version** is stamped by the Market Engine and preserved verbatim downstream. |
| 7 | **Market State** is a fact, never a buy/sell decision. |
| 8 | The **Market Engine** computes facts, never decisions or signals. |
| 9 | **Normalization** is owned at the Data Provider boundary; the engine consumes normalized data. |
| 10 | **Immutable Context** is a property that must hold for every MarketContext. |

### Strategy Engine & Interpretation
| # | Rule |
|---|------|
| 11 | **Strategy** always means a plug-in that interprets Features; it never measures the market. |
| 12 | A **Strategy** never accesses brokers or the Data Provider directly. |
| 13 | A **Strategy** never mutates a MarketContext. |
| 14 | **StrategyResult** always means the immutable output of one Strategy evaluation. |
| 15 | A **StrategyResult** is never mutated after creation. |
| 16 | **Score** is a standardized representation whose meaning is strategy-owned. |
| 17 | **Score** never means **Confidence**, and vice versa. |
| 18 | **Ranking** always means the authoritative ordering owned by the Strategy Engine. |
| 19 | **Ranking** never changes or re-computes a **Score**. |
| 20 | **Ranking** is never re-sorted or re-computed by the API, transport, or frontend. |
| 21 | **Plugin** always refers to a Strategy added via the contract without editing the engine. |
| 22 | Adding a **Strategy** never modifies the Strategy Engine. |
| 23 | **Diagnostics** are observability data, never part of a result's meaning. |

### Boundaries & Architecture
| # | Rule |
|---|------|
| 24 | **Repository** never contains business logic. |
| 25 | **Service** contains business logic; the **API** layer does not. |
| 26 | The **API** layer never accesses persistence except through a Repository. |
| 27 | **Event Bus** always means the decoupled, typed, fire-and-forget delivery mechanism (a contract, not a technology). |
| 28 | **WebSocket / WS** always means the transport that delivers change; it never computes, scores, or re-ranks. |
| 29 | **Clean Architecture** implies dependencies point inward; the term is never used to justify an outward dependency. |
| 30 | **Layer** always carries a single responsibility and the defined dependency direction. |
| 31 | **Async First** means blocking calls never run on the event loop. |
| 32 | **Adapter** always means a translator to/from an external contract, owned at a boundary. |
| 33 | **Data Provider** always means the broker abstraction layer; brokers never leak above it. |

### Data, State & Storage
| # | Rule |
|---|------|
| 34 | **Source of Truth** always means PostgreSQL (ADR-001). |
| 35 | **Cache** (incl. Redis Cache, Memory Cache) is never the Source of Truth. |
| 36 | **TTL** always refers to a cached value's bounded lifetime, never to persistent data. |
| 37 | **Migration** always means a versioned, reviewed schema change; never a hand-applied edit. |
| 38 | **Entity** always means a persistent domain object accessed via a Repository. |
| 39 | **JSONB** refers to the storage type governed by the JSONB usage policy, not arbitrary blob storage. |
| 40 | **Server State** and **client/UI State** are distinct and never conflated in the frontend. |

### Interface, Context & Session
| # | Rule |
|---|------|
| 41 | **Context** is always qualified (e.g., MarketContext, Session Context); bare "context" is avoided where meaning matters. |
| 42 | **Session** (connection) and **Trading Session** (market timing) are never used interchangeably. |
| 43 | **Version** in the API sense always means the explicit major contract version. |
| 44 | **Idempotency** always means repeat-safe operations yielding the same end state. |
| 45 | **Pagination**, **Filtering**, and **Sorting** operate on declared dimensions only, never client-supplied logic. |
| 46 | **Authentication** (who) and **Authorization** (what) are distinct and never swapped. |

### Governance & AI
| # | Rule |
|---|------|
| 47 | **ADR** always means a binding recorded decision; it is superseded, never silently overwritten. |
| 48 | **RFC** always means a proposal discussed before implementation. |
| 49 | **Architecture Freeze** means the architecture changes only via ADR/RFC. |
| 50 | **Definition of Done** and **Acceptance Criteria** are distinct: DoD is completion criteria, Acceptance is verifiable satisfaction of intent. |
| 51 | **Deprecated terms (§14) must not appear** in new documents, code, or AI prompts. |
| 52 | A **new architectural term is added to this glossary before** it is used anywhere else. |
| 53 | **AI assistants** (Codex, Claude, ChatGPT, future) must use these definitions exactly. |
| 54 | The words **"signal", "indicator", and "snapshot"** are never substituted for Feature, StrategyResult, or MarketContext. |
| 55 | Where this glossary and another document disagree on a term's meaning, **the glossary governs meaning** (the other document is corrected). |

---

## 17. Glossary Maintenance Checklist

100 items grouped by maintenance concern. Run the relevant group whenever the documentation set,
architecture, code, or AI prompts change.

### New Terms
- [ ] Every new architectural term is added here before first use elsewhere.
- [ ] Each new term has a canonical single-sentence definition.
- [ ] Each new core term has Purpose, Owner, Related Terms, and Used In.
- [ ] The new term is placed in the correct domain section.
- [ ] The new term is added to the Cross-Reference Matrix (§15).
- [ ] The new term's related terms link back to it.
- [ ] The new term does not collide with an existing definition.
- [ ] The new term avoids a deprecated synonym.
- [ ] The new term's owner is identified.
- [ ] The new term is referenced by at least one architecture document.

### Deprecated Terms
- [ ] Replaced terms are moved to the Synonyms & Deprecated table (§14).
- [ ] Each deprecation records the preferred term and the reason.
- [ ] Deprecated terms are removed from new documents.
- [ ] Deprecated terms are removed from code names when touched.
- [ ] Deprecated terms are removed from AI prompts/templates.
- [ ] No deprecated term is reintroduced without an ADR.
- [ ] Deprecation history is preserved (not silently deleted).
- [ ] Ambiguous synonyms ("signal", "indicator", "snapshot") remain listed as avoided.
- [ ] Each deprecated term maps to exactly one preferred term.
- [ ] The deprecation is announced in the change that makes it.

### Cross-References
- [ ] The Cross-Reference Matrix lists each high-traffic term.
- [ ] Each term's "Referenced Documents" list is accurate.
- [ ] Each term's "Primary Owner" is correct.
- [ ] Related-term links resolve to defined terms.
- [ ] Section anchors used in links are valid.
- [ ] Renamed sections update all inbound references.
- [ ] Removed terms are removed from the matrix.
- [ ] The matrix ordering aids lookup.
- [ ] Cross-document term usage matches the matrix.
- [ ] Duplicate matrix entries are removed.

### ADR Synchronization
- [ ] Every concept named in an ADR is defined here.
- [ ] New ADR concepts are added to the glossary.
- [ ] Superseded ADR terminology is reconciled here.
- [ ] ADR-001's "Source of Truth" definition matches §8.
- [ ] No ADR contradicts a glossary definition.
- [ ] ADR references in the glossary are correct.
- [ ] Decision-driven term changes are reflected here.
- [ ] The ADR index and glossary agree on names.
- [ ] New ADRs (002+) are scanned for new terms.
- [ ] ADR rationale is not duplicated in the glossary (only meaning is owned here).

### Architecture Synchronization
- [ ] Every capitalized term in `00`–`12` is defined here.
- [ ] Architecture documents use terms per these definitions.
- [ ] Boundary terms (facts vs decisions, features vs signals) are consistent across docs.
- [ ] Diagram labels use canonical terms.
- [ ] Section titles use canonical terms.
- [ ] A term redefined locally in a document is corrected.
- [ ] New architecture components have glossary entries.
- [ ] Renamed components update the glossary and all docs.
- [ ] Owner assignments match the architecture documents.
- [ ] "Used In" lists match reality across the doc set.

### Implementation Synchronization
- [ ] Code names for glossary concepts map unambiguously to terms.
- [ ] Module/component names reflect canonical terms.
- [ ] Event/type names reflect canonical terms.
- [ ] No code introduces a term absent from the glossary.
- [ ] Deprecated terms are absent from new code.
- [ ] Renamed code concepts trigger a glossary review.
- [ ] Public API/contract names align with API terms (§10).
- [ ] Frontend state names align with §9 (server vs UI state).
- [ ] Repository/service names align with §3/§7.
- [ ] Migrations/entities align with §8 terms.

### AI Prompt Synchronization
- [ ] AI prompt templates use canonical terms.
- [ ] AI system context references this glossary.
- [ ] AI is instructed to add new terms here first.
- [ ] AI is instructed never to use deprecated synonyms.
- [ ] AI-generated names are reviewed against the glossary.
- [ ] AI is told the glossary governs meaning in conflicts.
- [ ] AI usage of "signal/indicator/snapshot" is caught in review.
- [ ] AI is bound to `11` §17 alongside this glossary.
- [ ] AI-introduced terms trigger a glossary update.
- [ ] AI prompt updates accompany glossary changes.

### Documentation Synchronization
- [ ] The glossary is updated in the same change as a term's introduction.
- [ ] Documentation drift against the glossary is treated as a defect.
- [ ] The mini-TOC and section numbering stay consistent.
- [ ] Tables render correctly after edits.
- [ ] Callouts/notes/warnings remain accurate.
- [ ] The banner rule (new term first) is upheld.
- [ ] Versioning of definitions is additive/clarifying where possible.
- [ ] Genuine meaning changes are recorded in §14.
- [ ] The document date/status is current.
- [ ] Links to other docs resolve.

### Consistency Audits
- [ ] A periodic audit confirms no undefined capitalized terms in `00`–`12`.
- [ ] A periodic audit confirms no deprecated terms in current docs.
- [ ] A periodic audit confirms code names match the glossary.
- [ ] A periodic audit confirms AI prompts match the glossary.
- [ ] Score vs Confidence usage is audited.
- [ ] Feature vs Signal usage is audited.
- [ ] Ranking-never-re-scores is audited across docs and code.
- [ ] Source-of-Truth usage is audited (Cache never authoritative).
- [ ] Session vs Trading Session usage is audited.
- [ ] Context is always qualified in audited material.

### Ownership & Governance
- [ ] Each term has a clear primary owner.
- [ ] Ownership changes update the glossary.
- [ ] Terminology-rule violations are logged in review.
- [ ] The non-negotiable rules (§16) remain ≥50.
- [ ] New boundary terms get a non-negotiable rule if warranted.
- [ ] The glossary is referenced by the review checklist (`11` §18).
- [ ] The glossary is referenced by onboarding material.
- [ ] Conflicts escalate to Architecture, not resolved ad hoc.
- [ ] The glossary version aligns with the doc-set version.
- [ ] This checklist itself is reviewed when the process changes.

---

## 18. Summary

### 18.1 What This Document Is

`13_ARCHITECTURE_GLOSSARY.md` is the **canonical vocabulary reference** for ApexScan — the single source
of truth for what every architectural term means. It defines terms across ten domains (core, market data,
market engine, strategy engine, backend, database, frontend, API, deployment, AI), catalogs abbreviations,
records deprecated synonyms, indexes terms in a cross-reference matrix, codifies **55 non-negotiable
terminology rules**, and provides a **100-item maintenance checklist** that keeps the vocabulary
synchronized with the ADRs, the architecture, the code, and AI prompts.

### 18.2 What It Owns and What It Never Owns

| Owns | Never Owns |
|------|------------|
| The meaning of every architectural term | Decisions and their rationale (owned by ADRs) |
| The canonical name for each concept | What the system does (owned by `00`–`12`) |
| Deprecated-term migration | How anything is computed (no formulas) |
| The cross-reference index | Implementation or code |
| Terminology rules and maintenance process | Business/trading logic |

### 18.3 Terminology Consistency Assessment

Maintaining **one canonical vocabulary** is not documentation hygiene — it is load-bearing for the whole
project:

- **For architecture:** the boundaries that give ApexScan its guarantees (facts vs decisions, features vs
  signals, transport vs authorship, cache vs source of truth) are encoded *in the words*. Consistent terms
  keep those boundaries legible and enforceable across thirteen documents.
- **For implementation:** when code names map unambiguously to glossary terms, a reader can move between
  design and source without translation, and reviewers can catch a boundary violation by catching a
  mis-used word.
- **For AI-generated code:** Codex, Claude, and ChatGPT are only as precise as the vocabulary they are
  given. A shared glossary turns a correctly-termed prompt into correctly-termed, boundary-respecting
  code, and lets reviewers flag drift the instant a forbidden synonym appears.
- **For future maintenance:** contributors arriving years later inherit not just the design but the exact
  meaning of every term in it — so the architecture is understood as intended, not re-interpreted.

**Why one vocabulary is essential:** ApexScan's correctness depends on many independent parts agreeing on
what they are doing. Documents agree on *what*; the engineering manual agrees on *how*; the ADRs agree on
*why*. This glossary ensures they all agree on *what the words mean* — the foundation the other three kinds
of agreement rest on. With it in place, **any contributor, human or AI, speaks one language**, and the
architecture survives contact with many hands over many years.

---

*End of `13_ARCHITECTURE_GLOSSARY.md` — Official Vocabulary Reference for ApexScan.*
*Governing rule: any future document that introduces a new architectural term must update this glossary first.*
