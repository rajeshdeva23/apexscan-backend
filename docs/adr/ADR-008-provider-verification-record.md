# ADR-008 D4 — Provider-Semantics Verification Record (Dhan live session OHLC)

| Field | Value |
|-------|-------|
| **Records** | The ADR-008 D4 provider-verification gate for the DhanHQ live session-OHLC aggregate |
| **Governs** | Whether production may inject `SessionStatisticsAuthority(provider_aggregate_verified=True)` for the Dhan live composition |
| **Verification date** | 2026-08-11 |
| **Verdict** | **NOT SUFFICIENT — production authority remains disabled (fail closed)** |
| **Status** | Open (re-run required once the missing provider guarantees are documented) |

> This is an evidence record, not an architecture decision. ADR-008 (Accepted, immutable)
> is unchanged. No production code or authority default was modified as a result of this
> verification.

## Sources inspected

| Type | Source |
|------|--------|
| OFFICIAL_PROVIDER_DOC | DhanHQ v2 — Live Market Feed (`https://dhanhq.co/docs/v2/live-market-feed/`): QUOTE (feed response code 4) and FULL (code 8) packet field layouts; Previous-Close packet (code 6). |
| REPOSITORY_CODE | `app/adapters/dhan/live.py` (`_QUOTE_PAYLOAD`, `_FULL_PAYLOAD`, `_decode_quote_packet`, `_decode_full_packet`, `_session_ohlc`); `app/market_engine/session_statistics.py`, `tick_engine.py` (authority default). |
| — | NSE exchange documentation: not consulted, because the gap is Dhan's *field scope*, not NSE's open-price definition; NSE docs cannot establish what value Dhan places in `Day Open Value` (that would be INFERENCE). |
| CONTROLLED_LIVE_OBSERVATION | None. No live probe performed (no authorized credential-gated live-smoke evidence was used; not required to reach the verdict). |

## Field layout — PROVEN (doc ↔ code agree exactly)

The official QUOTE packet order is `LTP, LTQ, LTT, ATP, Volume, TotalSellQty, TotalBuyQty, Day Open, Day Close, Day High, Day Low`; the FULL packet inserts `OI, HighestOI, LowestOI` before the same `Day Open, Day Close, Day High, Day Low`. This matches `_QUOTE_PAYLOAD`/`_FULL_PAYLOAD` and the `_session_ohlc(day_open, day_high, day_low, day_close)` mapping byte-for-byte. **No mapping bug; no packet-layout discrepancy.** The Previous-Close packet (code 6) is decoded-and-ignored, matching the doc.

## Verification matrix (ADR-008 D4)

| # | Property | Classification | Evidence type | Reason |
|---|----------|----------------|---------------|--------|
| 1 | NSE cash-equity applicability | PROVEN | REPOSITORY_CODE | The scanner universe and Dhan segment mapping are NSE cash-equity (ADR-004); the feed applies to those instruments. |
| 2 | QUOTE carries OHLC | PROVEN | OFFICIAL_PROVIDER_DOC + REPOSITORY_CODE | Doc lists `Day Open/Close/High/Low Value` in code-4 packet; code decodes them. |
| 3 | FULL carries OHLC | PROVEN | OFFICIAL_PROVIDER_DOC + REPOSITORY_CODE | Same fields in code-8 packet; code decodes them. |
| 4 | Production TICK mode carries OHLC | PROVEN | REPOSITORY_CODE | `DhanLiveFeedMode` has only QUOTE/FULL and `_feed_mode_for` accepts only those; both carry OHLC, so no accepted production mode omits it. |
| 5 | Regular-session open semantics | **NOT_PROVEN** | OFFICIAL_PROVIDER_DOC | Field labelled only "Day Open Value"; scope (regular-session open vs pre-open/indicative) is **NOT STATED**. |
| 6 | Pre-open exclusion | **NOT_PROVEN** | OFFICIAL_PROVIDER_DOC | No statement that pre-open/opening-auction values are excluded from the day OHLC. |
| 7 | Running session/day high | **NOT_PROVEN** | OFFICIAL_PROVIDER_DOC | Labelled "Day High Value"; not documented as an exchange cumulative day-to-date traded high. |
| 8 | Running session/day low | **NOT_PROVEN** | OFFICIAL_PROVIDER_DOC | Labelled "Day Low Value"; not documented as an exchange cumulative day-to-date traded low. |
| 9 | Mid-session subscription coverage | **NOT_PROVEN** | OFFICIAL_PROVIDER_DOC | No statement that the first packet after a mid-session subscribe carries the pre-subscription day OHLC. |
| 10 | Feed-gap independence | **NOT_PROVEN** | OFFICIAL_PROVIDER_DOC | No statement that the aggregate remains exchange-cumulative independent of locally observed trades. |
| 11 | Reconnect/resubscribe coverage | **NOT_PROVEN** | OFFICIAL_PROVIDER_DOC | No statement that a fresh packet after reconnect carries current day-to-date values. |
| 12 | Trading-day reset | **NOT_PROVEN** | OFFICIAL_PROVIDER_DOC | No statement that day OHLC resets per trading day. |
| 13 | Trade-statistic (not order-book) | CONDITIONALLY_PROVEN | OFFICIAL_PROVIDER_DOC | "Day Open/High/Low" sit with LTP/ATP/Volume trade fields (distinct from the depth block), strongly implying trade statistics — but not stated verbatim; condition is not machine-enforceable. |
| 14 | Independence from local observation | **NOT_PROVEN** | OFFICIAL_PROVIDER_DOC | Whether the values are exchange-provided cumulative vs connection-computed is **NOT STATED** — the crux of ADR-008 Model B. |

## Safety-critical properties still NOT_PROVEN

5 (regular-session open), 6 (pre-open exclusion), 7 (running high), 8 (running low), 9 (mid-session coverage), 10 (feed-gap independence), 11 (reconnect coverage), 12 (trading-day reset), 14 (local-observation independence).

Per ADR-008 D4 and the fail-closed rule, one NOT_PROVEN safety-critical property is sufficient to withhold authority; here **nine** are unproven. The gap is uniform: the official documentation specifies the packet *fields* but not their *scope or cumulative/reconnect/mid-session semantics*. Field labels must not be upgraded to guarantees.

## Decision

Production authority is **NOT enabled**. The effective production state remains
`SessionStatisticsAuthority(provider_aggregate_verified=False)` (the immutable generic
default). No Dhan composition injects verified authority. Current-day historical
reconciliation remains disabled (`supports_current_day=False`; unchanged).

`Tick.session_ohlc` continues to be transported canonically (P4.6A) and the Market
Engine continues to compute `SessionStatistics` gated by the injected authority (P4.6B/C);
because authority is disabled, the resulting statistics are `None`/non-authoritative and
no strategy may treat them as authoritative.

## To close this gap (future re-run)

Obtain, from official DhanHQ (or DhanHQ engineering) and/or an authorized controlled
live observation, documentary evidence that the day OHLC is an exchange-provided
cumulative session statistic that: excludes pre-open/indicative values; carries the
full day-to-date high/low; is delivered in full on a mid-session subscribe and after a
reconnect/resubscribe; is independent of locally observed trades; and resets per
trading day. Then re-run ADR-008 D4 and, only if every safety-critical property reaches
PROVEN (or CONDITIONALLY_PROVEN with a machine-enforceable, already-satisfied condition),
enable verified authority in the Dhan composition layer (never via a provider branch in
the Market Engine).

## Evidence-closure re-check — 2026-08-19 (CURRENT-SESSION-OHLC-AUTHORITY-EVIDENCE-CLOSURE-R1)

Re-inspected all repository evidence for the WebSocket/tick-carried source. **No change.**

- `NEW_L1_EVIDENCE = NONE`; `DHAN_L2_RESPONSE = NOT_AVAILABLE` (no attributable DhanHQ response recorded).
- `L3_AUTHORIZATION = NOT_GRANTED`; `L3_EXECUTION = NOT_RUN`; no credential read, no authenticated WebSocket.
- The nine mandatory tick-source semantic properties remain **NOT_PROVEN** (only field carriage proven). `TICK_AUTHORITY_GATE = FAILED / NOT SUFFICIENT`.
- Source separation intact: REST evidence cannot enable `tick_aggregate_verified`. Production `tick_aggregate_verified=False`, `supports_current_day=False` (verified in code). Fail closed.
- No production code changed.
