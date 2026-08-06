# ADR-004 — Use NSE Cash Equity as the V1 Live Scanner Domain

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-06 |
| **Deciders** | Platform / Market Data Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Related** | `docs/05_DATA_PROVIDER.md`, `docs/06_MARKET_ENGINE.md`, `docs/07_STRATEGY_ENGINE.md`, `docs/12_ROADMAP.md`, ADR-003 |

---

## Context

ApexScan V1 is a real-time scanner for meaningful activity and momentum in an
underlying stock. The validated production NSE F&O universe contains 208
canonical equity underlyings. Each is structurally eligible because the
instrument master contains one or more `FUTSTK` and/or `OPTSTK` contracts for
that underlying.

F&O eligibility does not by itself choose the live instrument whose market data
the scanner observes. A production scanner domain must be explicit before the
Data Provider establishes live subscriptions, because the resulting canonical
events become inputs to the future Market Engine.

## Problem

Subscribing indiscriminately to derivative contracts would add contract-specific
semantics to the V1 scanner: futures expiry rolls and basis, or option strike
and expiry selection. It would also greatly expand the number of subscriptions.
Conversely, treating a provider's security identifier as the application
identity would leak a broker-specific locator beyond the Data Provider boundary.

The system needs one clear V1 market-observation domain that keeps F&O
eligibility structurally derived while preserving the broker-neutral contracts
defined by ADR-003.

## Decision

For ApexScan V1, the production eligible universe is the validated set of NSE
equity underlyings with `FUTSTK` and/or `OPTSTK` contracts. The live scanner
subscribes to the linked **NSE cash-equity** instrument for each eligible
underlying.

`FUTSTK` and `OPTSTK` contracts determine eligibility; they are not the
primary live scanner subscription domain. The current validated production
universe contains 208 underlyings.

```text
F&O eligibility
        ↓
208 canonical NSE equity underlyings
        ↓
validated NSE cash-equity master row
        ↓
adapter-private provider reference
        ↓
canonical live market data
        ↓
Market Engine
```

## Decision Drivers

- The scanner's purpose is to assess the underlying stock.
- Cash equity avoids futures expiry-roll complexity and futures-basis effects.
- Cash equity avoids option strike and expiry selection, and the resulting
  contract explosion.
- The 208-underlying universe fits within one standard Dhan live-feed
  connection, subject to the provider's documented subscription batching
  limits.
- F&O eligibility remains structurally derived from `FUTSTK` and `OPTSTK`
  instrument-master relationships.
- Derivative contract selection can be introduced later without changing the
  scanner's primary V1 market-observation domain.

This is a V1 scanner-domain decision; it does not assert that cash equity is
superior for every future strategy or market-data use case.

## Eligible-Universe Definition

An underlying is eligible only when the validated instrument-master
relationship establishes it as an NSE equity underlying with one or more
`FUTSTK` and/or `OPTSTK` contracts. The eligibility derivation is structural;
it is not a manual whitelist or a symbol-pattern blacklist.

Duplicate futures expiries and option strikes remain distinct provider
contracts. Deduplication occurs only at the canonical underlying-universe level.

## Live-Subscription-Domain Definition

For every eligible underlying, the Data Provider resolves exactly one real NSE
cash-equity master row. Live subscriptions use that cash-equity row's
adapter-private provider reference. The V1 scanner does not subscribe to:

- all futures contracts;
- all option contracts;
- nearest-expiry futures as its primary domain; or
- selected option strikes as its primary domain.

Before opening a production live subscription, the mapping gate must establish:

| Check | Required result |
|-------|-----------------|
| Production F&O-eligible underlyings | 208 |
| Cash-equity mappings found | 208 |
| Missing mappings | 0 |
| Ambiguous mappings | 0 |
| Canonical-symbol mismatches | 0 |

Any missing, ambiguous, or mismatched mapping is a safe failure: it must be
reported by canonical symbol and not silently dropped or guessed.

## Why Cash Equity Is Selected

The V1 scanner evaluates the activity of the underlying NSE equity rather than
a particular derivative contract. Cash-equity observations avoid introducing
futures-specific roll and basis effects into that assessment. They also avoid
requiring an option strike or expiry decision, avoiding thousands of
contract-specific subscriptions before there is a strategy requirement for
them.

## Role of FUTSTK and OPTSTK

`FUTSTK` and `OPTSTK` are essential to the domain, but only as evidence that an
NSE equity belongs in the F&O-eligible universe. They remain available for
future derivative-specific research, strategy, and execution capabilities.
They do not determine the primary live data contract observed by the V1
scanner.

## Provider-Reference Mapping

The mapping is:

```text
canonical underlying
        ↓
validated NSE cash-equity master row
        ↓
adapter-private Dhan provider reference
        ↓
live subscription
```

The Dhan security identifier is a provider locator, not canonical application
identity. It stays within the Dhan adapter and is never exposed through
canonical market-data contracts, the Market Engine, Strategy Engine, API, or
frontend.

## Market Engine Input Implications

Phase 4 Market Engine consumes broker-neutral canonical live data representing
the NSE cash-equity underlying for this approved scanner universe. It must not
infer that a V1 source event represents a futures or option contract.

If a future Market Engine or strategy requires derivative-specific facts, that
is a separate capability and domain decision. This ADR preserves the broker
neutrality and canonical-event boundary required by ADR-003.

## Strategy Engine Implications

The Strategy Engine remains broker-neutral and consumes Market Engine facts,
not Dhan payloads or provider identifiers. This decision does not add strategy
logic or imply derivative-selection logic in Phase 5. Strategies may later use
derivative-specific facts only through separately approved, canonical
capabilities.

## Future Derivative-Selection Implications

Futures and options remain valid future domains. A later decision may introduce
an appropriate contract-selection policy for a named consumer, such as a
strategy, research workflow, or execution feature. That decision must define
its own data semantics and must not retroactively redefine the V1 cash-equity
scanner stream.

## Explicitly Out of Scope

This ADR does not decide:

- which option strike to trade;
- CE versus PE selection;
- which futures expiry to trade;
- order execution or a derivatives execution engine;
- option Greeks;
- futures-basis models;
- portfolio or risk logic; or
- Phase 5 strategy logic.

It also does not introduce Dhan-specific concepts above the Data Provider
boundary, database changes, frontend changes, or Market Engine implementation.

## Consequences

### Benefits

- One deterministic primary instrument per eligible underlying makes the V1
  scanner's market-observation domain clear.
- The subscription universe remains bounded at 208 cash-equity instruments,
  while derivative eligibility remains evidence-based.
- Provider identifiers and protocol details remain contained in the adapter.
- The future Market Engine receives canonical underlying-equity events without
  contract-specific derivatives semantics.
- Derivative capabilities can be added deliberately without changing the V1
  scanner's meaning.

### Trade-offs

- V1 does not directly observe futures basis, expiry-specific liquidity, option
  pricing, Greeks, or option-chain activity.
- The provider must maintain a verified one-to-one mapping from each eligible
  underlying to its cash-equity reference.
- Future derivative-aware features require a separate data and domain decision
  instead of reusing V1 observations by assumption.

## Alternatives Considered

| Alternative | Decision | Why |
|-------------|----------|-----|
| NSE cash equity for F&O-eligible underlyings | Chosen | Observes the underlying stock while retaining a bounded, structurally derived universe. |
| Nearest-expiry stock futures | Rejected for the V1 primary scanner domain | Introduces expiry-roll and basis complexity. |
| Selected option contracts | Rejected for the V1 primary scanner domain | Requires strike/expiry selection and creates a large contract universe. |
| Mixed cash and derivatives scanner domain | Deferred | Increases scope and requires a future strategy/domain decision to justify its semantics. |

## Future Evolution

Future derivative-specific research, Market Engine facts, strategies, or
execution capabilities may add futures or option observations through new,
explicit domain decisions. Such work may select expiries, strikes, or depth
requirements appropriate to its use case, but it must preserve the
broker-neutral Data Provider boundary and must not reinterpret the V1
cash-equity stream as derivative data.

## Relationship to Existing ADRs and Governing Documents

### ADR-003 — Broker Adapter Pattern

ADR-003 remains unchanged. This ADR specifies the V1 business-domain choice of
which canonical underlying instruments are observed; ADR-003 specifies that
the provider-specific resolution, subscription protocol, and normalization stay
inside the Broker Adapter/Data Provider boundary.

### Data Provider, Market Engine, and Roadmap

`docs/05_DATA_PROVIDER.md` continues to own connection, subscription,
instrument-master, and normalization mechanics. `docs/06_MARKET_ENGINE.md`
continues to own broker-neutral fact production from canonical inputs.
`docs/12_ROADMAP.md` continues to sequence Data Provider work before the Market
Engine and Strategy Engine. This ADR adds no phase work and does not modify
their responsibilities.

---

*This ADR records a point-in-time decision. If it is ever revised, mark it
`Superseded by` a new ADR rather than editing the decision in place.*
