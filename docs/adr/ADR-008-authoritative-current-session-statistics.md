# ADR-008 — Authoritative Provider-Supplied Current-Session Statistics

| Field | Value |
|-------|-------|
| **Status** | Accepted |
| **Date** | 2026-08-10 |
| **Deciders** | Platform / Market-Engine Architecture |
| **Supersedes** | — |
| **Superseded by** | — |
| **Refines** | `docs/06_MARKET_ENGINE.md` (§6 MarketContext, §7 session state, §17 session statistics, §19 events/ordering, §25 non-fabrication), `docs/05_DATA_PROVIDER.md` (§7 subscription types, §10 canonical data model) |
| **Related** | ADR-003 (broker-adapter pattern), ADR-004 (NSE cash-equity V1 domain), ADR-005 (canonical session cumulative volume), ADR-006 (candle completeness / feed continuity / reconciliation), ADR-007 (strategy lifecycle & requirements), `docs/07_STRATEGY_ENGINE.md` (§7 contract, §16 dependencies) |

---

## Context

`docs/06_MARKET_ENGINE.md` §17 assigns the Market Engine ownership of current-session
statistics — **Opening Price**, **Today's High**, **Today's Low**, **Session
Extremes** — as facts carried in the MarketContext (§6.3 "Market Statistics"), and
§17.1 states they are "owned solely by the Market Engine; readers consume, never
write." The implemented MarketContext, however, does **not** populate these as
authoritative typed facts. The only current-session OHLC available today is the live
candle engine's `PartialCandle`, which is **non-authoritative by construction**: per
ADR-006 a finalized live interval is never authoritative from snapshots, and the
session partial's `open_price` is merely the first tick *observed by this process*.

The Phase-5 preflight for the first concrete strategy (Open=High) STOPPED for exactly
this reason: it cannot prove the true regular-session opening price or the running
session high/low under **mid-session startup**, **missed packets**, or **feed
disconnect/reconnect**, because:

- the session `PartialCandle` is locally observed (Model A) and carries no coverage
  guarantee — a process that starts at 11:30 sees an "open" of the 11:30 price;
- current-day historical reconciliation is explicitly withheld and **NOT PROVEN**
  (`HistoricalWarmupService(supports_current_day=False)`; the range planner resolves
  windows over *previous completed sessions* only);
- fabricating the missing facts is forbidden (`docs/06` §25 "mark unavailable, never
  fabricate"; ADR-005/006).

Concrete strategies must not reconstruct these facts themselves (`docs/07` §4.11,
§7.4). The fix is to provide the fact in the MarketContext — a Market-Engine concern.

The P4.6 design/preflight established that the fact *source already exists on the
wire*: the Dhan v2 live feed's **Quote (response code 4)** and **Full (response code
8)** packets both carry a **provider-supplied current-session OHLC aggregate**
(`day_open`, `day_high`, `day_low`, `day_close`), which the adapter currently decodes,
validates, and then **discards** — the same decoded-then-discarded pattern that
ADR-005 promoted for session cumulative volume, and which ADR-006's Context already
acknowledges ("Ticker/Quote/Full binary packets carrying aggregate fields such as
day-cumulative volume … and day OHLC"). This is a **provider session aggregate**
(Model B), fundamentally different from a locally reconstructed extremum (Model A):
it is re-sent on every packet, so the first packet after a mid-session start or a
reconnect already carries the session-to-date open/high/low.

This ADR governs promoting that aggregate to a broker-neutral canonical fact and the
Market-Engine-owned `SessionStatistics` fact derived from it. It records only the
decision; implementation is the P4.6 slices.

## Decision Drivers

- The Market Engine must expose the §17 facts; the Strategy Engine must never rebuild
  them (`docs/07` §4.11, §7.4; rule 1).
- Authority must be **provable**, not assumed — a locally observed extremum is not an
  authoritative full-session fact (`docs/06` §25; ADR-006).
- Additive evolution only: adding or surfacing a canonical fact must not require a
  MarketContext redesign, a Strategy-Manager change, or a Data-Provider contract-shape
  change (ADR-003; `docs/07` rule 30).
- Determinism, per-instrument ordering, and one-version-per-datum are preserved
  (`docs/06` §5, §11, §19.4).
- No fabrication across mid-session start or feed gap (ADR-005/006).
- Broker neutrality: provider packet semantics stay inside the adapter (ADR-003).
- The generic fact must serve future strategies (Open=Low and others) with no further
  Market-Engine change.

## Decision

### D1 — Canonical provider session aggregate

Introduce an additive, broker-neutral canonical value representing the
provider-supplied **current-session OHLC** aggregate — conceptually
`ProviderSessionOhlc` with `open_price`, `high_price`, `low_price`, `close_price`
(exact repository-consistent naming decided in P4.6A). It carries **no** provider
field names, packet/response codes, security identifiers, routing data, or
strategy-specific data. The provider-specific mapping (e.g. Dhan `day_open` → canonical
`open_price`) stays **adapter-private**, exactly as ADR-005 keeps the volume mapping
private.

### D2 — Canonical owner: an optional field on `Tick`

The aggregate is carried as an optional field on the canonical `Tick`:
`Tick.session_ohlc: ProviderSessionOhlc | None`. Rationale: the aggregate co-arrives
with trade data in the same accepted packet; the V1 quote-mode feed already emits only
a `Tick` (ADR-005); the canonical `Quote` is order-book state and `DepthSnapshot` is
unrelated. This mirrors ADR-005's `session_cumulative_volume` ownership precedent. The
aggregate is **not** duplicated on `Quote` or any other canonical contract.

`Tick.last_price`, `Tick.traded_quantity`, `Tick.session_cumulative_volume`, `Quote`,
`DepthSnapshot`, and `Candle` are **unchanged and not reinterpreted**. The addition is
purely additive and optional (absent when a provider does not report it).

### D3 — Authority model (Model A vs Model B)

Two models are explicitly distinguished and never silently merged:

- **Model A — local observation**: the first tick observed, or `max`/`min` over
  observed ticks. Incomplete under mid-session start, missed packets, and feed gaps.
- **Model B — provider session aggregate**: the provider-supplied current-session OHLC
  snapshot (D1).

Only a **verified Model B** aggregate (D4) may produce an `AUTHORITATIVE`
`SessionStatistics`. Local observations are **never** promoted to authoritative
full-session facts.

### D4 — Provider verification gate (mandatory, P4.6D)

Before `AUTHORITATIVE` may be enabled in production, authoritative provider/exchange
evidence must establish, for the NSE cash-equity V1 domain (ADR-004):

1. the live aggregate applies to the same NSE cash-equity instrument/session domain;
2. `open` is the **regular-session opening price** required by Open=High/Open=Low
   (not a pre-open indicative price);
3. `high`/`low` are **session-to-date extrema** over that same regular session;
4. the statistics **reset per trading day/session**;
5. the values are **session aggregates independent of whether ApexScan observed every
   trade**;
6. the **V1 QUOTE-mode feed** carries the fields;
7. a **reconnect / new subscription returns current aggregate state**, not only
   post-connect observations.

If these cannot be sufficiently proven, **STOP P4.6D**: `AUTHORITATIVE` must not be
claimed, the semantics must not be weakened to make implementation pass, and this ADR's
authority model must be revisited before production use.

### D5 — Market-Engine-owned `SessionStatistics` fact

The Market Engine owns an immutable fact — conceptually `SessionStatistics` with
`trading_date`, `open_price`, `high_price`, `low_price`, `quality`, and `as_of` (the
provider event time of the snapshot). It contains **no** signals, no Open=High/Open=Low
status, no score, no rank, and no provider metadata — it is a market fact only (§17
Note: "session statistics are facts, not verdicts").

### D6 — Quality model

The initial quality model has exactly two states:

- `AUTHORITATIVE` — a verified Model-B aggregate (D3/D4) is present and valid for the
  current regular session.
- `UNAVAILABLE` — no verified aggregate is available, or the session phase does not
  permit regular-session statistics.

`AUTHORITATIVE` is legal **only** after the D4 gate succeeds. No speculative additional
states are introduced. If D4 fails, `AUTHORITATIVE` is not used and governance is
revisited (a tainted/incomplete state would then be required).

### D7 — Regular-session open, running high, running low

- `open_price` is the authoritative opening price of the governed NSE regular trading
  session. It is **not** the first ApexScan tick, the first tick after startup,
  `PartialCandle.open`, a previous-session open, or a pre-open indicative price.
- `high_price` is the authoritative session-to-date maximum; `low_price` the
  authoritative session-to-date minimum — for the same governed regular session, from
  the verified provider aggregate, **never** derived solely from locally observed
  `last_price` values.

### D8 — Session-phase eligibility (pre-open / auction / halt / holiday / closed)

Before `LIVE_SESSION`, regular-session `SessionStatistics` must **not** become
`AUTHORITATIVE` from indicative/pre-open values; `PRE_OPEN` and `OPENING_AUCTION` are
never collapsed into regular-session statistics. During `HOLIDAY`, `MARKET_CLOSED`, and
`EMERGENCY_HALT` no new statistics are synthesized without accepted market data; a
previously `AUTHORITATIVE` snapshot **may remain** the last-known fact (with its `as_of`
unchanged), but no new extrema are invented. `CLOSING_SESSION` may retain the last
authoritative session values.

### D9 — Mid-session startup and feed gap

- **Mid-session startup:** `AUTHORITATIVE` may be established immediately **only** if
  the first accepted verified provider aggregate proves the true regular-session
  open/high/low from the beginning of that session; otherwise authority is withheld
  (`UNAVAILABLE`). The missing period is **never** replaced with local observations.
- **Feed gap / reconnect:** consistent with ADR-006, a `RECONNECTED` continuity fact
  **alone** does not restore authority. A fresh accepted verified provider aggregate
  may restore `AUTHORITATIVE` **only** because its Model-B semantics (D4) cover the
  session independently of local packet continuity. ADR-006's candle-completeness and
  volume rules are unchanged and not weakened.

### D10 — Session reset

`SessionStatistics` resets on the canonical `trading_date` transition (P4.3 session
semantics), exactly as the candle engine resets per session. It is **never** reset by
application restart, strategy restart, or provider reconnect. No previous-day statistics
leak into a new session.

### D11 — Validation, ordering, and one-version-per-datum

- **Structural invariants** (when established): `high ≥ low`; `low ≤ open ≤ high`;
  canonical price validity; same instrument as the enclosing `Tick`; no provider
  sentinel values cross the adapter boundary. Canonical monotonic high/low rules are
  **not** made stronger than the provider contract (legitimate provider corrections may
  occur); the Market Engine's handling of stale/regressive aggregates (drop, never
  regress fresher extrema) is explicit implementation policy, not a canonical invariant.
- **Stale / duplicate:** the existing per-instrument canonical event ordering governs;
  a stale aggregate must not overwrite fresher `SessionStatistics`. No provider sequence
  numbers escape the adapter; no second global ordering system is created.
- **One packet → one version:** one accepted live provider packet → one canonical
  `Tick` carrying `session_ohlc` → **at most one** ordinary MarketContext version. The
  tick, session, session-statistics, candle, and historical facts are stamped
  atomically into that single context update. A packet must never mint a separate
  statistics-only version.

### D12 — Determinism / replay

Given the same recorded `Tick` stream (including `session_ohlc`), session configuration,
continuity facts, and prior state, the Market Engine produces identical
`SessionStatistics`, MarketContext versions, and events. No wall-clock read, randomness,
provider lookup, or network call occurs inside the Market Engine (`as_of` is the
provider event time).

### D13 — Current-day historical reconciliation unchanged

This ADR does **not** authorize enabling current-day Historical Context repair.
`supports_current_day = False` and `CURRENT_DAY_RECONCILIATION_GUARANTEE = NOT PROVEN`
remain in force (ADR-006 §12). Live provider-aggregate authority is a **separate**
capability from historical reconciliation.

### D14 — Ownership split and provider neutrality

- **Data Provider adapter** owns: provider packet decoding, provider-specific
  validation, and mapping provider fields to the canonical `ProviderSessionOhlc`. Dhan
  specifics (`day_open`/`day_high`/`day_low`, packet codes 4/8, security IDs) live only
  in the adapter and its verification evidence.
- **Market Engine** owns: `SessionStatistics` state, session-phase semantics,
  authority/quality, trading-date reset, and MarketContext integration.
- **Strategy Engine** owns: interpretation of these facts only.

Future providers may map an equivalent verified session aggregate to the same canonical
contract without any Market-Engine change.

### D15 — Strategy-neutrality (Open=High / Open=Low)

The Market Engine never emits strategy semantics — no `open_high_valid`,
`OPEN_HIGH_MATCH`, signal, score, or rank. Open=High remains a Strategy Engine plug-in
that reads `context.session` and `context.session_statistics` and performs its own
comparison (`docs/07` §7; ADR-007 D15). The **same** `SessionStatistics`
`open_price`/`low_price` facts later support Open=Low with **no** Market-Engine or
schema change — demonstrating the fact's strategy-neutrality. This ADR implements
neither strategy.

### D16 — Memory and persistence

`SessionStatistics` is ephemeral current Market-Engine state, bounded to **one current
statistics state per instrument** (~208 for the V1 universe). There is no PostgreSQL
table, no Redis persistence, and no historical-statistics store. ADR-001 (PostgreSQL as
the durable source of truth for persisted domain data) remains intact.

## Normative example (mid-session start)

ApexScan starts at 11:30 IST. The first accepted QUOTE-mode packet carries the
provider's session aggregate (`day_open` = the 09:15 regular-session open, `day_high`
and `day_low` = the session-to-date extrema). Given the D4 verification, the Market
Engine stamps `SessionStatistics(open, high, low, quality=AUTHORITATIVE)` into that
tick's single MarketContext version. Open=High can then compare `high_price` to
`open_price` without reconstructing anything. Had D4 not held, `quality` would be
`UNAVAILABLE` and Open=High would SKIP.

## Normative example (feed gap)

Feed connected from the open; disconnect 10:30–10:35; reconnect. The `RECONNECTED`
fact alone restores nothing. The first accepted post-reconnect packet re-carries the
current cumulative session OHLC → `SessionStatistics` returns to `AUTHORITATIVE`. No
local reconstruction of the 10:30–10:35 window occurs; candle completeness and volume
remain governed by ADR-006, unchanged.

## Consequences

**Positive:** the §17 facts become real, authoritative, and broker-neutral; mid-session
start and feed-gap correctness are structurally provided by the Model-B aggregate; the
Strategy Engine stays a pure consumer; one additive fact serves Open=High, Open=Low, and
future strategies with no further Market-Engine change; determinism, ordering, and
one-version-per-datum are preserved; historical reconciliation is untouched.

**Negative / accepted:** the production authority claim depends on the D4 provider
verification gate (a mandatory, potentially blocking evidence task); the initial quality
model is deliberately two-state and may need a tainted state if D4 reveals the provider
value is locally reconstructed; one optional field is added to the canonical `Tick` and
one optional field to `MarketContext`.

## Alternatives considered

- **Read the live session `PartialCandle` open/high in strategies.** Rejected:
  non-authoritative, coverage-unverifiable, and it pushes a Market-Engine fact into the
  strategy layer (the P5.6 blocker).
- **Enable current-day historical reconciliation.** Rejected: `supports_current_day`
  is `NOT PROVEN`; this ADR must not weaken that guarantee (D13), and the live provider
  aggregate solves the need without it.
- **Reconstruct extrema locally from observed ticks.** Rejected: fabrication forbidden
  by ADR-005/006 and `docs/06` §25; incorrect under mid-session start and feed gaps.
- **A separate `SessionStatistics` canonical event minting its own MarketContext
  version.** Rejected: violates one-packet/one-version (D11) and complicates ordering;
  the aggregate rides the same accepted tick.
- **New canonical fact without a governing ADR.** Rejected: ADR-005/006 precedent is a
  dedicated ADR per new broker-neutral canonical fact, and §17 currently defines no
  completeness/quality model for session statistics — a governance gap this ADR closes.

## Governance acceptance (answers now explicit)

1. Source of authority: **provider session aggregate (Model B)**, D1/D3. 2. Canonical
owner: **`Tick.session_ohlc`**, D2. 3. Existing contracts: **unchanged/additive**, D2.
4. Market-Engine fact: **`SessionStatistics`**, D5. 5. Quality: **AUTHORITATIVE /
UNAVAILABLE**, D6. 6. Open semantics: **regular-session open**, D7. 7. High/low:
**session-to-date extrema**, D7. 8. Pre-open/auction: **not authoritative**, D8. 9.
Mid-session start: **authoritative only if the first verified aggregate proves it, else
withheld**, D9. 10. Feed gap/reconnect: **reconnect alone restores nothing; a fresh
verified aggregate may**, D9. 11. Session reset: **canonical trading-date transition**,
D10. 12. One packet → one version: **yes**, D11. 13. Replay: **deterministic**, D12.
14. Current-day reconciliation: **stays disabled / NOT PROVEN**, D13. 15. Provider
neutrality: **adapter-private mapping**, D14. 16. Strategy neutrality: **no
signal/score/rank emitted**, D15. 17. Open=Low reuse: **same fact, no schema change**,
D15. 18. Persistence: **ephemeral, bounded per instrument; ADR-001 intact**, D16. 19.
Production authority: **gated on D4 verification**. 20. Relationship to ADR-005/006:
**complementary; ADR-006 not weakened**.
