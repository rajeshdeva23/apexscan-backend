# ADR-003 — Adopt the Broker Adapter Pattern

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-04 |
| **Deciders** | Platform / Market Data Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | `docs/01_SYSTEM_ARCHITECTURE.md`, `docs/03_BACKEND_ARCHITECTURE.md`, `docs/05_DATA_PROVIDER.md`, `docs/06_MARKET_ENGINE.md`, `docs/07_STRATEGY_ENGINE.md`, `docs/11_CODING_GUIDELINES.md`, `docs/12_ROADMAP.md`, `docs/13_ARCHITECTURE_GLOSSARY.md`, ADR-001, ADR-002 |

---

## Context

Brokers are external, volatile dependencies. Their authentication, payloads,
protocols, instruments, rate limits, and failure behaviour differ and change
independently of ApexScan. The frozen architecture already defines the Data
Provider as the boundary that contains this volatility before data reaches the
Market Engine.

`docs/05_DATA_PROVIDER.md` describes the Broker Adapter Pattern as a required
architectural seam, but the decision record referenced there was not authored.
This ADR records the existing decision and its governance rules; it does not
introduce a new architecture.

## Problem

Allowing a concrete broker SDK, payload, identity, or failure model to cross
into the Market Engine, Strategy Engine, API, or frontend would couple the
platform to that broker. A broker replacement or addition would then require
unrelated core changes and violate the Dependency Rule.

## Decision

ApexScan uses a broker-independent **Broker Adapter Pattern**. Each external
broker/provider is represented by a broker-specific adapter implementing shared
provider/adapter contracts. The adapter normalizes provider payloads into
canonical broker-independent contracts, which are the only market-data contract
available above the Data Provider boundary.

```text
External Broker / Provider
        ↓
Broker-Specific Adapter
        ↓
Normalization
        ↓
Canonical Provider Contracts
        ↓
Data Provider Layer
        ↓
Market Engine
        ↓
Strategy Engine
```

Dhan is one adapter implementation, not the architecture. Adding or replacing
a broker is additive and must not require Market Engine, Strategy Engine, API
contract, or frontend changes merely because the provider implementation differs.

## Decision Drivers

- Isolate the platform from broker API and SDK volatility.
- Keep Market Engine and Strategy Engine broker-blind.
- Normalize facts once at the external boundary.
- Make additional providers additive and contract-tested.
- Contain provider failures without corrupting downstream data.
- Preserve async, typed, testable boundaries.

## Responsibilities and Ownership

### Broker Adapter

Each broker adapter may know its broker's SDK/API, authentication mechanism,
payload shape, rate limits, subscription mechanics, and protocol failures. It
translates those concerns into shared contracts and must not know the Market
Engine, Strategy Engine, application services, persistence, API, or frontend.

### Data Provider

The Data Provider layer owns the uniform façade and cross-cutting provider
concerns: adapter coordination, connection lifecycle, subscriptions, health,
historical loading, instrument loading, authentication orchestration, and
normalization. Its consumers depend on canonical contracts, never on Dhan or
another named provider.

### Normalization and Canonical Contracts

Normalization is the hard boundary between broker-shaped input and ApexScan
market-data contracts. The Data Provider owns canonical broker-neutral market
data contracts; adapters produce them; the Market Engine consumes them. Broker
SDK types, raw payload objects, and provider-specific field names do not cross
this boundary.

## Dependency Direction and Isolation Rules

- The Market Engine never imports a broker SDK or a named broker adapter.
- The Strategy Engine never imports a broker SDK or Data Provider implementation.
- API and frontend contracts never expose broker SDK types or broker payloads.
- Upper layers depend only on canonical provider/adapter contracts.
- No upper layer contains conditional logic based on broker identity.
- Broker-specific credentials remain centralized, provider-specific runtime
  configuration; they are never hard-coded, logged, or exposed to upper layers.
- Provider failures are translated to provider-independent failure categories at
  the boundary where applicable. Secrets and raw sensitive payload data never
  appear in errors.
- One failing adapter is isolated from other adapters and the core.

## Testing Implications

Every adapter must satisfy the same contract tests. Tests must prove that a fake
adapter and a concrete adapter are substitutable, canonical outputs contain no
broker SDK types, and broker-specific data does not leak above the boundary.
External provider APIs are mocked or represented by sanitized recorded fixtures
in ordinary tests; live-provider validation is opt-in and never a general CI
dependency.

## Consequences

### Benefits

- A broker can be added or replaced without core-engine, strategy, API, or
  frontend rewrites.
- Broker-specific volatility is confined to small, replaceable modules.
- Normalized data gives every downstream consumer one stable vocabulary.
- Contract tests expose boundary regressions before integration.
- Provider faults are isolated and represented honestly instead of fabricating
  market data.

### Trade-offs

- Each provider requires mapping and maintenance code in addition to its SDK.
- Canonical contracts must be deliberately evolved and contract-tested.
- The first adapter must resist Dhan-specific shortcuts even when they appear
  faster in the short term.

## Alternatives Considered

| Alternative | Why rejected |
|-------------|--------------|
| Let the Market Engine call a concrete broker SDK | Couples core facts to provider protocol and prevents substitutability. |
| Put broker-specific conditional branches in upper layers | Spreads volatility and violates broker-blind boundaries. |
| Implement Dhan first and generalize later | Makes Dhan the accidental architecture instead of proving the shared seam. |
| Expose raw broker payloads through API/frontend contracts | Makes external clients depend on unstable provider details. |
| Add a dynamic plugin marketplace or multi-broker failover now | These are documented future evolutions, not prerequisites for the adapter boundary. |

## Migration and Implementation Implications

Phase 3 implements this decision incrementally: first the canonical contracts
and adapter contract, then provider lifecycle and concrete adapter behavior.
No existing application code is migrated by this ADR. New implementations must
follow the dependency, isolation, configuration, and contract-testing rules
above; a violation requires architectural review rather than an exception.

## Future Evolution

Additional providers, simultaneous adapters, provider failover, and load sharing
remain future Data Provider concerns. They extend this seam without changing the
Market Engine or Strategy Engine. This ADR does not authorize a plugin
marketplace, dynamic loading system, or multi-broker failover implementation.

## Relationship to Existing ADRs

### ADR-001 — PostgreSQL as the Source of Truth

This decision does not change storage ownership. If normalized instrument
reference data is durable, PostgreSQL remains authoritative and access remains
behind repositories/services. Adapters never write a store directly.

### ADR-002 — Separate ApexScan into Backend and Frontend Repositories

The backend repository owns broker adapters, Data Provider implementation,
provider configuration, tests, and canonical architecture documentation. The
frontend remains an independent consumer of backend HTTP/WebSocket contracts and
does not receive provider SDK types or depend on a backend checkout.

## Governing References

- `docs/01_SYSTEM_ARCHITECTURE.md` §§2.3, 2.9, 4.7
- `docs/03_BACKEND_ARCHITECTURE.md` §§3.9, 5.4, 23, 27, 28
- `docs/05_DATA_PROVIDER.md` §§2–14
- `docs/06_MARKET_ENGINE.md` §§9, 14, 30.4
- `docs/07_STRATEGY_ENGINE.md`
- `docs/11_CODING_GUIDELINES.md` §§3, 4, 15, 18, 19
- `docs/12_ROADMAP.md` §6 and §18
- `docs/13_ARCHITECTURE_GLOSSARY.md` §§3–5

---

*This ADR records a point-in-time decision. If it is ever revised, mark it
`Superseded by` a new ADR rather than editing the decision in place.*
