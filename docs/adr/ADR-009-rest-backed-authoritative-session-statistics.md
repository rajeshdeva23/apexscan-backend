# ADR-009 — REST-Backed Authoritative Current-Session Statistics

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-11 |
| **Deciders** | Platform / Market-Engine Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Complements** | ADR-008 (Authoritative Provider-Supplied Current-Session Statistics) |
| **Refines** | `docs/06_MARKET_ENGINE.md` (§6 MarketContext, §7 session, §17 session statistics, §19 events/ordering, §25 non-fabrication), `docs/05_DATA_PROVIDER.md` (§7 subscription/quote), `docs/07_STRATEGY_ENGINE.md` (§7 contract, §16 dependencies) |
| **Related** | ADR-003 (broker adapter), ADR-004 (NSE cash-equity V1), ADR-005 (session cumulative volume), ADR-006 (candle completeness / feed continuity), ADR-007 (strategy lifecycle & requirements) |

---

## Context

ADR-008 introduced `ProviderSessionOhlc`, `Tick.session_ohlc`, `SessionStatistics`,
`SessionStatisticsQuality`, and `MarketContext.session_statistics`, with authority gated
on verifying the provider-supplied **WebSocket Day OHLC** aggregate carried on the tick.

P4.6D verified that DhanHQ QUOTE/FULL packets carry `Day Open/Close/High/Low` and that
the P4.6A mapping is byte-exact, but that the **safety-critical WebSocket semantics**
(regular-session open, pre-open exclusion, cumulative high/low, mid-session coverage,
feed-gap independence, reconnect coverage, trading-day reset, independence from local
observation) are **not sufficiently documented**. Production session-statistics authority
therefore remains disabled (`ADR-008-provider-verification-record.md`).

P4.6D.1/P4.6E established that Dhan's **Market Quote REST** API (`/marketfeed/ohlc`,
`/marketfeed/quote`) documents an `ohlc` object with stronger "of the day" semantics and,
being a stateless server query, is structurally a full-day server-side snapshot —
resolving by construction the connection-local gaps the WebSocket stream cannot document
(mid-session coverage, feed-gap independence, reconnect independence, local-observation
independence). A REST response is **not** a `Tick` and must not be fabricated into one; it
requires a new generic canonical input path and an independent source.

This ADR governs that path. It **complements** ADR-008 and does not rewrite it: ADR-008's
`ProviderSessionOhlc`, `Tick.session_ohlc` transport, `SessionStatistics`,
`MarketContext.session_statistics`, Market-Engine ownership, non-fabrication rule, and
current-day historical isolation all remain in force. ADR-008's Tick-carried authority
model is not removed; it remains available should the WebSocket semantics later pass
verification.

## Decision Drivers

- Authoritative current-session open/high/low must be establishable **independently of
  local feed coverage** (mid-session start, feed gaps, reconnects) — the P4.6 motivation.
- The Market Engine stays the **sole writer** of MarketContext and **provider-blind**
  (docs/06 §6.4; ADR-003).
- Strategies remain **read-only** consumers; they never fetch (docs/07 §4.11).
- Determinism and one-datum/one-version are preserved (docs/06 §5/§11/§19).
- No fabrication; fail closed (ADR-005/006; docs/06 §25).
- Additive evolution only; each new canonical fact via its own ADR (ADR-005/006 precedent).
- Reuse the Phase-4 async-staging precedent (`install_historical` → surface on next datum)
  and the P5.4 requirement-registry lifecycle rather than inventing new mechanisms.

## Decision

### D1 — Canonical `SessionStatisticsObservation`

Introduce an additive, immutable, broker-neutral canonical **input**:
`SessionStatisticsObservation` with `instrument: Instrument`, `trading_date: date`,
`observed_at: datetime` (UTC), `session_ohlc: ProviderSessionOhlc`. It carries **no**
provider name, security id, REST URL, packet code, strategy id, signal, score, rank,
refresh cadence, or credentials. `ProviderSessionOhlc` (ADR-008 D1) is **reused verbatim**
as the OHLC payload; it is not duplicated and not overloaded with provider identity or
freshness.

### D2 — Independent `SessionStatisticsSource` port

Govern a broker-neutral source capability (a `SessionStatisticsSource` port) that loads
current-session statistics for a collection of canonical instruments and returns
`SessionStatisticsObservation`s. Properties: provider-neutral, batch-capable, may perform
asynchronous provider I/O **outside** the Market Engine, mutates no MarketContext, knows
no strategy, and knows no ranking/dedup. Concrete implementations live in the
adapter/composition layers, never in the Market Engine. (Exact method signatures are an
implementation-design concern, not fixed by this ADR.)

### D3 — Ownership

- **Provider adapter** — provider request, decoding, provider-field validation, provider→
  canonical identity mapping, `ProviderSessionOhlc` construction. Provider names/codes/ids
  stay adapter-private.
- **Composition/service** — refresh scheduling, batching, coalescing, capability selection,
  `observed_at` timestamping where the provider supplies none, source activation/
  deactivation, and authority-capability wiring.
- **Market Engine** — staging, bounded per-instrument state, phase/trading-date validation,
  stale protection, whole-snapshot reconciliation, `SessionStatistics` creation/update, and
  MarketContext stamping.
- **Strategy** — read-only interpretation only. No provider calls.

### D4 — Market-Engine staging ingestion

A `SessionStatisticsObservation` is **staged** into bounded per-instrument Market-Engine
state. Staging itself **mints no MarketContext version, publishes no event, fabricates no
Tick, and mutates no already-published MarketContext**. The observation becomes visible on
the **next accepted canonical market datum** for that instrument, mirroring the existing
historical-context staging model (`install_historical` → surface on next datum).

### D5 — One-datum/one-version preserved

One accepted canonical market datum → **at most one** MarketContext version. When a staged
observation exists, the next accepted datum stamps latest tick/quote, session, candles,
historical, **and** session statistics into one immutable context. No statistics-only
version; no `SessionStatisticsUpdated` event (unless separately governed later).

**Latency consequence (accepted):** a fresh REST observation may not be visible until the
next accepted datum for that instrument. This is an intentional consistency trade-off; no
second version-publisher is introduced to shorten it.

### D6 — Authority vs freshness (separated)

**Source authority** answers "can this source semantically establish the official
current-session open/high/low?" **Snapshot freshness** answers "is this authoritative
snapshot recent enough for a particular consumer?" These are **distinct**:
`SessionStatisticsQuality.AUTHORITATIVE` never implies "fresh enough for every strategy".

- **Authority gate:** a source may produce `AUTHORITATIVE` statistics only after its
  provider semantics pass the required verification gate (ADR-008 D4, re-run for the source).
  Canonical-shape compatibility, `ProviderSessionOhlc` validity, and field labels alone are
  insufficient. Production remains fail-closed until verification succeeds.
- **Quality states unchanged:** `SessionStatisticsQuality` stays two-state
  (`AUTHORITATIVE`/`UNAVAILABLE`). **No `STALE` state is added** merely because a consumer's
  freshness window expired — quality is provenance/completeness; freshness is a
  consumer-relative temporal eligibility evaluated against `observed_at`.
- **Freshness model:** no absolute duration is fixed by this ADR. A consumer needing
  freshness declares a governed maximum age; if a snapshot exceeds it, the fact remains
  `AUTHORITATIVE` (provenance) but is **not usable** for that consumer.

### D7 — REST-vs-WebSocket precedence

Until the WebSocket `Tick.session_ohlc` semantics independently pass verification, the
WebSocket aggregate is **transported but UNVERIFIED**: it **must not overwrite or downgrade**
an authoritative REST-derived `SessionStatistics`. Field-by-field merges are **forbidden**
(no `open` from REST + `high` from WebSocket; no `max`/`min` across sources). Every
authoritative update is one coherent snapshot from one verified source observation. The
generic principle: **only verified sources may establish authoritative statistics**; if
multiple sources later become verified, source selection requires an explicit deterministic
precedence policy — never a silent merge.

### D8 — Stale / out-of-order observations

Stale/out-of-order observations must never regress authoritative state. Ordering is by
`observed_at` (or another explicit canonical ordering field) — **never** async task
completion order. The **whole-snapshot invariant** (ADR-008/P4.6B) holds: open/high/low are
accepted as one coherent snapshot; no field-by-field merge, `max`/`min` reconstruction, or
synthetic correction.

### D9 — Failure and malformed-response behavior

On source/provider failure: no fabricated observation, no local-extrema reconstruction, no
promotion of WebSocket provisional data to authority; retain the last authoritative snapshot
with its **original `observed_at`** (never refreshed on failure) — freshness eligibility
expires naturally per the consumer policy. A malformed/structurally invalid provider OHLC
fails closed: no observation staged, no partial-field merge, no `last_price` repair (the
adapter rejects it; the Market Engine only receives valid canonical observations).

### D10 — Refresh model and rate-limit governance

Refresh is **infrastructure-level**: shared across strategies, batch-capable,
rate-limit-aware, coalesced, bounded, and activated only when required — no per-strategy
polling loop and no per-instrument request storm where provider batching exists. This ADR
prescribes **no** absolute polling interval or instrument-count constant; provider limits
(currently ~1 request/second, up to ~1000 instruments/request for Dhan Market Quote) are
recorded as **adapter/service capability constraints**, not generic architecture law.

### D11 — Declarative requirement `FactNeed.SESSION_STATISTICS`

A strategy that needs session statistics declares `FactNeed.SESSION_STATISTICS` (additive to
the P5.1 `FactNeed` vocabulary); it does **not** activate any provider directly. The P5.4
requirement layer computes the effective union of declaring strategies and the infrastructure
activates/deactivates the shared refresh source accordingly — no strategy-name branching.
(Not implemented by this ADR.)

Lifecycle follows ADR-007/P5.4: START acquires the requirement; PAUSE retains it and the
session footprint; RESUME does not re-acquire; ERROR retains until an explicit STOP; STOP and
FORCE STOP release it; shared consumer requirements survive an individual STOP; last-consumer
release may deactivate the refresh. **Dependency activated ≠ dependency satisfied:** a strategy
becomes operationally ready only when a usable observation exists (correct session +
`AUTHORITATIVE` + fresh enough), not merely because the refresh started.

On last-consumer release, already-published MarketContexts remain immutable; whether the
current per-instrument latest-statistics state is retained or cleared is a narrow policy
**deferred to implementation** under the existing requirement/state-lifecycle precedent.

### D12 — Deterministic replay

Provider I/O occurs only outside the replay-deterministic Market Engine. The
`SessionStatisticsObservation` is a **recorded canonical input**. Replay consumes recorded
observations and performs no REST calls; the same observation sequence + canonical
market-data sequence + session/calendar inputs produce identical `SessionStatistics`,
MarketContext versions, events, and strategy evaluations. `observed_at` is never generated by
wall-clock access inside core logic.

### D13 — Bounded state

At most one staged observation plus one current `SessionStatistics` per instrument. No
unbounded REST-observation history in the Market Engine; no persistent statistics store is
authorized (ADR-001 unaffected).

### D14 — Current-day historical isolation

`HistoricalWarmupService.supports_current_day == False`, `CURRENT_DAY_WITHHELD`, and
`CURRENT_DAY_RECONCILIATION_GUARANTEE == NOT PROVEN` remain in force. REST current-session
statistics are a **live authoritative source**, never historical reconciliation; ADR-008 and
ADR-006 remain intact.

### D15 — Strategy boundary

Open=High/Open=Low remain ordinary strategy plug-ins that read only `MarketContext.session`
and `MarketContext.session_statistics`. They must not read Dhan REST, the
`SessionStatisticsSource`, `ProviderSessionOhlc` directly, adapter objects, or provider
security ids. The Market Engine emits no `OPEN_HIGH_MATCH`/`OPEN_LOW_MATCH`/signal/score/rank.

## Startup / session semantics

- **Mid-session startup (e.g. 11:30):** no local reconstruction; the refresh source obtains
  the current provider-side day snapshot, which is staged and surfaces on the next datum.
  Dependent strategies stay not-ready/`SKIPPED` until it is visible and satisfies authority +
  freshness.
- **Session start / pre-open:** before valid regular-session statistics exist →
  `UNAVAILABLE`; pre-open indicative values are never promoted. At/after `LIVE_SESSION` the
  first verified observation may establish statistics; a response is not required exactly at
  09:15:00.
- **Reconnect:** WebSocket reconnect and REST-source continuity are independent — a WS
  reconnect does not invalidate a fresh authoritative REST snapshot, and a REST failure is not
  "repaired" merely because the WebSocket reconnected.

## Provider verification (residual)

Enabling `AUTHORITATIVE` for the REST source requires closing the residual Dhan REST evidence
per `ADR-008-provider-evidence-closure-plan.md`: official-open equivalence, pre-open
exclusion, day-high/low cumulative semantics, and trading-day reset. These remain
`CONDITIONALLY_PROVEN`/`NOT_PROVEN`; they are **not** upgraded here. Authority stays disabled;
controlled observations may supplement documentary evidence but do not silently replace a
required provider-contract guarantee, and credential use needs explicit authorization.

## Implementation sequence (post-acceptance)

P4.6E1 canonical `SessionStatisticsObservation` → P4.6E2 Market-Engine staging + deterministic
reconciliation → P4.6E3 `SessionStatisticsSource` port + Dhan REST batch adapter → P4.6E4
shared refresh/coalescing service → P4.6E5 `FactNeed.SESSION_STATISTICS` + lifecycle activation
+ freshness support → **P4.6E6 provider-authority verification and production enablement
(LAST)**. Authority enablement is always last; no earlier slice enables it.

## Acceptance invariants

1. A REST response is never fabricated into a `Tick`.
2. The Market Engine remains provider-blind (`scan_market_engine == {}`).
3. Strategies remain provider-blind.
4. The canonical observation is broker-neutral.
5. Staging mints no MarketContext version.
6. No statistics-only event.
7. One accepted market datum → at most one MarketContext version.
8. Authority and freshness are distinct.
9. An unverified source cannot establish `AUTHORITATIVE`.
10. No field-by-field source merge.
11. A stale observation cannot regress state.
12. Provider failure never fabricates freshness (`observed_at` not refreshed on failure).
13. Replay performs no provider I/O.
14. Bounded per-instrument state.
15. Current-day historical reconciliation remains disabled.
16. Open=High/Open=Low remain strategy concerns.

## Consequences

**Positive:** mid-session startup no longer depends on local feed coverage; REST authority is
independent of WebSocket continuity; one shared fact serves Open=High/Open=Low/future
strategies; no provider leakage into the Market Engine; deterministic replay is preserved;
provider batching covers the whole ~208-instrument universe efficiently.

**Negative / accepted:** REST polling infrastructure and its rate-limit handling; staged
visibility latency (surface-on-next-datum); freshness-policy complexity; an added requirement
activation path; an external provider-contract dependency; an additional recorded input for
deterministic replay.

## Rejected alternatives

A. Fabricate a REST response into a `Tick`. B. Let Open=High call Dhan REST directly. C. Let
the Market Engine import/call Dhan REST. D. Reconstruct open/high/low from locally observed
ticks (Model A). E. Use `PartialCandle` as authoritative day statistics. F. Treat WebSocket
Day OHLC as authoritative despite the failed ADR-008 D4. G. Merge REST and WebSocket OHLC
field-by-field. H. Mint independent MarketContext versions from arbitrary REST polling without
separate governance. I. Use current-day historical reconciliation despite its NOT-PROVEN
status. J. Mark stale snapshots `UNAVAILABLE` by rewriting their provenance state without a
governed freshness model.

## Status of dependent work

- **Production session-statistics authority:** remains **disabled** (fail closed).
- **P5.6 Open=High:** remains **BLOCKED** — it is unblocked only after (1) REST provider
  semantics pass authority verification, (2) the source pipeline is implemented, (3)
  declarative requirement activation exists, (4) the strategy's freshness semantics are
  governed, and (5) integration is validated.
- **Open=Low:** the same generic source and `SessionStatistics` fact support it later with no
  Open=Low-specific schema/source/Market-Engine path.

## Governance acceptance (answers now explicit)

1. Canonical input: `SessionStatisticsObservation` (D1). 2. OHLC payload: reuse
`ProviderSessionOhlc` (D1). 3. Source: broker-neutral `SessionStatisticsSource` port (D2).
4. Ownership: adapter/composition/engine/strategy split (D3). 5. Ingestion: staging, surface
on next datum (D4). 6. Versioning: one datum → one version; no statistics-only event (D5).
7. Authority vs freshness: separated; two quality states retained (D6). 8. Precedence: verified
source authoritative; WS unverified never overwrites; no merge (D7). 9. Ordering: by
`observed_at`; whole-snapshot (D8). 10. Failure/malformed: fail closed; retain last with
original `observed_at` (D9). 11. Refresh: infrastructure-level, batched, rate-limit-aware; no
constants in architecture (D10). 12. Requirement: additive `FactNeed.SESSION_STATISTICS`,
union-activated, ADR-007 lifecycle (D11). 13. Replay: recorded input, no I/O (D12). 14. Memory:
bounded per instrument (D13). 15. Current-day historical: stays disabled (D14). 16. Strategy
boundary: read-only, provider-blind (D15). 17. Production authority: disabled pending
verification. 18. ADR-008: complemented, not rewritten.
