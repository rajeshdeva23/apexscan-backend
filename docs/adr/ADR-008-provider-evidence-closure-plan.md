# ADR-008 D4 — Provider Evidence-Closure Plan (Dhan session statistics)

| Field | Value |
|-------|-------|
| **Purpose** | Define the strongest safe path to close the ADR-008 D4 gaps recorded in `ADR-008-provider-verification-record.md` |
| **Date** | 2026-08-11 |
| **Status** | Plan (no gap closed to a sufficient level yet; production authority remains disabled) |
| **Governs** | Nothing by itself — this is a research/verification-design artifact. ADR-008 (immutable) and all production code are unchanged. |

## 1. Where we stand

Proven (P4.6D): QUOTE/FULL packets carry `Day Open/Close/High/Low Value`; the P4.6A
mapping is byte-exact; production feed uses only QUOTE/FULL (both carry OHLC); the
Market Engine is fail-closed with authority disabled by default.

Still NOT_PROVEN for the **websocket** `Tick.session_ohlc` path: regular-session open,
pre-open exclusion, cumulative high, cumulative low, mid-session coverage, feed-gap
independence, reconnect coverage, trading-day reset, and independence from local
observation — the Dhan Live Market Feed page documents field *labels* but no *scope*.

## 2. New evidence gathered (P4.6D.1)

- **Dhan Market Quote REST** (`/marketfeed/ohlc`, `/marketfeed/quote`, v2) documents an
  `ohlc` object with *explicit* semantics — `open` = "Market opening price of the day",
  `high` = "Day High price", `low` = "Day Low price", `close` = "Market closing price of
  the day", plus `last_price`, `volume` ("Total traded volume for the day"),
  `average_price` ("VWAP of the day"), `net_change`. Rate limit 1 req/s, ≤1000
  instruments/request, no time-of-day restriction. **This is stronger than the websocket
  documentation** and, being a stateless server query, is structurally a full-day
  server-side snapshot (not connection-local). `OFFICIAL_PROVIDER_DOC`.
- **NSE (official)**: "The equilibrium price determined in pre-open session is considered
  as the open price for the day"; the pre-open call auction runs 09:00–09:15 and normal
  continuous trading opens at 09:15. `OFFICIAL_EXCHANGE_DOC`.

## 3. Expected regular-session open reference (the oracle)

`EXPECTED_REGULAR_SESSION_OPEN` for an NSE cash-equity instrument on trading day `D` :=
the pre-open call-auction **equilibrium price** (the price maximising executable volume,
with NSE's documented tie-breaks) discovered in the 09:00–09:15 pre-open and designated
by NSE as the day's official open, first effective at the 09:15 regular-session start.
A Dhan `Day Open` value PASSES the open-semantics check only if it equals this value
(to canonical price precision) and never carries a merely-indicative pre-open price.

## 4. Evidence hierarchy

- **L1** — Official provider documentation explicitly guarantees the semantics.
- **L2** — Official provider engineering/support written, attributable confirmation.
- **L3** — Repeated controlled observation against an independent authoritative reference.
- **L4** — Single observation or inference (insufficient).

Production `AUTHORITATIVE` enablement prefers **L1**; **L2** is acceptable if precise and
directly answers the safety-critical semantics; **L3** alone does not become a permanent
contract unless governance explicitly accepts observational verification; **L4** is
insufficient.

## 5. Minimum evidence required per guarantee

| Guarantee | Min level | Why | How to obtain |
|-----------|-----------|-----|---------------|
| Regular-session open | L1/L2 | Pre-open contamination risk; safety-critical | Dhan support Q1; REST doc ("open of the day") + NSE oracle as corroboration |
| Pre-open exclusion | **L2** | Cannot be inferred from a label | Dhan support Q2; pre-open live observation (L3) as support |
| Cumulative day high | L1/L2 | Model-B crux | Dhan support Q3; REST `high` doc |
| Cumulative day low | L1/L2 | Model-B crux | Dhan support Q4; REST `low` doc |
| Mid-session coverage | L2 + L3 | Must hold for any intraday start | Dhan support Q5; Test A |
| Feed-gap independence | **L2** | Hard to prove reliably by observation | Dhan support Q6/Q7 |
| Reconnect coverage | L2 + L3 | ADR-006 principle | Dhan support Q6; Test B |
| Trading-day reset | L1/L2 | Prevents cross-day leakage at source | Dhan support Q8; cross-day observation |
| Local-observation independence | **L2** | The Model-B contract itself | Dhan support Q7/Q10 |
| QUOTE == FULL semantics | L1/L2 | One `ProviderSessionOhlc` for both | Dhan support Q9 |

## 6. Dhan support/engineering questions (documentary answers requested)

For NSE cash equity, QUOTE (code 4) / FULL (code 8) websocket packets and the
`/marketfeed/ohlc`/`quote` REST responses:

1. Does `Day Open` represent the official current-day opening price determined by NSE's
   pre-open process?
2. Does it exclude merely-indicative/unexecuted pre-open prices?
3. Is `Day High` the current day's cumulative traded-price high from session start?
4. Is `Day Low` the current day's cumulative traded-price low from session start?
5. If a websocket subscription starts at 11:30, do `Day Open/High/Low` include trades
   from 09:15–11:30?
6. After a disconnect/reconnect, does the next packet contain the current full-day
   `Open/High/Low`, including activity during the disconnect?
7. Are these fields maintained server/exchange-side, independent of packets a given
   client connection observed?
8. Do they reset for each NSE trading day?
9. Are the QUOTE and FULL `Day OHLC` fields semantically identical?
10. Are these values derived from traded prices, not order-book/indicative values?
11. Do the websocket `Day OHLC` and REST `ohlc` object represent the same daily statistic?

## 7. Controlled live verification plan (design only — not executed this slice)

Runs only through the existing credential-gated harness
(`tests/integration/test_dhan_live_smoke.py`, gated by `APEXSCAN_DHAN_LIVE_SMOKE=1` +
`DHAN_LIVE_SMOKE_ENABLED=true`) with explicit user authorization and no secret printing.

- **Open-price check**: after pre-open completes, compare Dhan websocket `Day Open` and
  REST `ohlc.open` against `EXPECTED_REGULAR_SESSION_OPEN` for a chosen instrument →
  `OBSERVED_MATCH`/mismatch (L3 evidence, not a contract).
- **Test A (mid-session subscription)**: at a time well after open, (1) query REST
  `ohlc` as the independent current-day reference, (2) open a fresh websocket
  subscription, (3) capture the first `session_ohlc`, (4) compare open/high/low. First
  packet already carrying the earlier high/low supports mid-session coverage.
- **Test B (reconnect)**: capture OHLC → reconnect via the existing safe mechanism →
  capture first fresh `session_ohlc` → compare vs a fresh REST `ohlc`.
- **Test C (missed-packet independence)**: controlled disconnect interval; after
  reconnect, `Day High/Low` should reflect extrema reached during the local gap. If the
  market set no new extreme during the gap → `INCONCLUSIVE` (never fabricate movement).
- **Pre-open test (future date)**: capture pre-open values (if any) then the first
  `LIVE_SESSION` aggregate; confirm the value transitions to the official daily OHLC.
- **Trading-day reset test (cross-day)**: Day `N` final vs Day `N+1` first live aggregate;
  no carry-over. Mark `PENDING` if a same-turn cross-day test is impossible.

Observations are `L3` and support — never a substitute for the `L2` contract on
feed-gap independence and local-observation independence.

## 8. Architecture-safe alternatives (analysis only — none implemented)

- **A — Dhan REST Market Quote OHLC as the authoritative session snapshot.** Documented
  "day" semantics; stateless server query ⇒ inherently mid-session/reconnect/feed-gap
  independent. Strongest documentary basis today.
- **B — Another canonical provider** with explicit guarantees. Highest effort.
- **C — A generic `SessionStatisticsSource` port** (composition layer, outside the Market
  Engine) supplying verified canonical `SessionStatistics` independently of
  `Tick.session_ohlc` — the natural home for Alternative A. Keeps the Market Engine
  provider-blind (docs/07/ADR-003). **Preferred.**
- **D — Verify/enable current-day historical reconciliation** only if Dhan proves timely,
  complete current-day data (currently `NOT PROVEN`; out of scope here).
- **E — A directly exchange-authoritative source** if accessible.

**Preferred path:** pursue Dhan L2 confirmation (§6). If the *websocket* Day OHLC is
confirmed exchange-cumulative + reconnect-covering + local-observation-independent,
enable authority for the existing `Tick.session_ohlc` path. If only the **REST** `ohlc`
semantics can be confirmed, pursue **Alternative C fed by A** under a new governed slice
(a `SessionStatisticsSource` port), which likely reaches PASS with less ambiguity because
a REST snapshot is structurally Model B. Either way, ADR-008's Model-B requirement and
the fail-closed default are preserved; **Model A (local first-tick/observed extrema) is
rejected** for authoritative use, and no "best-effort Open=High" is introduced.

## 9. Pass criteria to re-run ADR-008 D4

Every safety-critical property (regular-session open, pre-open exclusion, cumulative
high, cumulative low, mid-session coverage, feed-gap independence, reconnect coverage,
trading-day reset, local-observation independence) reaches its §5 minimum level. Only
then may a Dhan **composition** layer inject `SessionStatisticsAuthority(provider_aggregate_verified=True)`
into the generic `TickEngine` (never a provider branch in the Market Engine), and the
generic default stays disabled.
