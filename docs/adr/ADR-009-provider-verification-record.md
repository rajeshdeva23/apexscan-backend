# ADR-009 E6 — REST Session-Statistics Provider Verification & Authority Gate

| Field | Value |
|-------|-------|
| **Records** | The ADR-008 D4 / ADR-009 E6 verification gate for the Dhan Market Quote REST OHLC source |
| **Governs** | Whether production may inject `SessionStatisticsAuthority(provider_aggregate_verified=True)` for the REST source |
| **Verification date** | 2026-08-11 |
| **Verdict** | **BLOCKED — production authority remains disabled (fail closed)** |
| **Status** | Open (re-run required once the missing guarantees are documented **and** per-source authority separation exists) |

> Evidence record only. ADR-008 and ADR-009 (both Accepted, immutable) are unchanged. No
> production code or authority default was modified as a result of this verification. No
> credentialed live probe was authorized or run.

## Sources inspected

| Type | Source |
|------|--------|
| OFFICIAL_PROVIDER_DOC | DhanHQ v2 — Market Quote (`https://dhanhq.co/docs/v2/market-quote/`): `POST /v2/marketfeed/ohlc`; `ohlc.{open,high,low,close}` field labels; ≤1000 instruments/request, 1 request/second; no response timestamp. |
| OFFICIAL_EXCHANGE_DOC | NSE — pre-open session: "the equilibrium price determined in pre-open session is considered as the open price for the day"; continuous trading opens 09:15 (recorded in `ADR-008-provider-evidence-closure-plan.md`). |
| REPOSITORY_CODE | `adapters/dhan/{adapter,normalizer}.py`, `services/session_statistics_{refresh,activation}.py`, `market_engine/{session_statistics,tick_engine,state,context}.py`, `strategy_manager/readiness.py`. |
| PROVIDER_SUPPORT | None on record. The §Support-questions below are unanswered. |
| CONTROLLED_LIVE_OBSERVATION | None. No credentialed live probe authorized by the E6 prompt. |

## Endpoint contract (re-confirmed)

`POST /v2/marketfeed/ohlc`, body `{segment: [security_id,…]}`, response
`data → segment → security_id → {last_price, ohlc:{open,close,high,low}}` + `status`;
≤1000 instruments/request, 1 req/s; **no timestamp** field. Unchanged since P4.6E3;
the P4.6A/E3 mapping remains correct. `ProviderSessionOhlc` structural invariants
(`high ≥ low`, `low ≤ open ≤ high`, `low ≤ close ≤ high`, positive) are consistent with
the documented fields (sentinel `0`s are withheld, not violations) — no canonical
validator change required (property J).

## Verification matrix (ADR-008 D4 / ADR-009 E6)

| # | Property | Classification | Level | Reason |
|---|----------|----------------|-------|--------|
| A | Official NSE day-open == `ohlc.open` | **NOT_PROVEN** | — | NSE defines the open (equilibrium), but the doc does not state Dhan `ohlc.open` equals it (NOT STATED). Label ≠ guarantee. |
| B | Pre-open / indicative exclusion | **NOT_PROVEN** | — | NOT STATED that a post-open `ohlc.open` excludes indicative pre-open values. |
| C | Running day-to-date traded high | **NOT_PROVEN** | — | "Day High price" is a label; cumulative-from-session-start not stated. |
| D | Running day-to-date traded low | **NOT_PROVEN** | — | "Day Low price" label; cumulative semantics not stated. |
| E | Mid-session coverage (pre-request trades) | CONDITIONALLY_PROVEN | structural + inferred | A stateless server query for "the day's" OHLC structurally returns whole-day values, but coverage-of-pre-request-trades is not documented; needs L2/L3. |
| F | WebSocket-observation independence | STRUCTURALLY_PROVEN | structural | REST is a separate server request path; its result cannot depend on this client's WS packet history. |
| G | Reconnect independence | STRUCTURALLY_PROVEN | structural | A WS disconnect/reconnect is unrelated to a REST request/response. |
| H | Trading-day reset | **NOT_PROVEN** | — | "of the day" is suggestive but per-day reset is NOT STATED. |
| I | Local-observation independence (Model B) | STRUCTURALLY_PROVEN | structural | The value is server-computed and returned per request, independent of local aggregation. |
| J | OHLC structural coherence | PROVEN | L1 + code | Documented fields fit the canonical invariants; no validator change. |

## Mandatory unresolved (safety-critical) properties

**A, B, C, D, H** remain NOT_PROVEN, and **E** is only CONDITIONALLY_PROVEN. These are
mandatory; per ADR-008 D4 / ADR-009 E6 a single unresolved mandatory property fails the
gate. Field labels and REST statelessness are **not** upgraded into provider guarantees.

## Verdict

- **ADR-008 D4 verdict:** NOT SATISFIED (REST source).
- **ADR-009 E6 verdict:** GATE FAILED.
- **Production authority decision:** NOT enabled. `SessionStatisticsAuthority(provider_aggregate_verified=False)` remains the effective state (`provider_aggregate_verified=True` absent from `app/`). SESSION_STATISTICS-declaring strategies are therefore never `AUTHORITATIVE` and stay not-ready — the expected fail-closed result.

## Per-source authority separation (independent hard blocker)

Even had the evidence passed, authority could not be safely enabled as built.
`resolve_session_statistics(..., authority=self._session_statistics_authority)` passes a
**single** `SessionStatisticsAuthority` to `apply_session_ohlc` for **both** the staged
REST observation and the WebSocket `tick.session_ohlc` aggregate. Setting
`provider_aggregate_verified=True` would elevate **both** paths — but the WebSocket path
is unverified (ADR-008 D4, P4.6D BLOCKED). This violates ADR-009 D7 (an unverified source
must not become authoritative). **PER-SOURCE AUTHORITY SEPARATION IS REQUIRED** before any
enablement: a broker-neutral, source-class-scoped capability (e.g. distinct
observation-source vs tick-aggregate verification), wired at composition — never a
provider branch in the Market Engine, never one shared boolean. If that separation
exceeds ADR-009 D7's current wording, it requires a governance note before implementation
(a prospective **E6A** slice).

## To close the gate (future re-run)

1. Obtain, from official DhanHQ documentation or attributable engineering/support (L2),
   or an authorized controlled observation (L3), proof of properties **A, B, C, D, H**
   (and firm E). See the questions and the observation plan below.
2. Implement **per-source authority separation** (E6A) so a verified REST source cannot
   elevate the unverified WebSocket aggregate.
3. Re-run E6; only if every mandatory property reaches its level **and** separation exists
   may composition inject verified authority for the REST source.

## Dhan support/engineering questions (unanswered)

For NSE equities, Market Quote `POST /v2/marketfeed/ohlc`: (1) Is `ohlc.open` the official
NSE day open from the pre-open mechanism? (2) After trading begins, can `ohlc.open` ever be
an indicative/pre-open-only value differing from the official open? (3) Is `ohlc.high` the
highest traded price from session start through the request? (4) Is `ohlc.low` the lowest
traded price from session start through the request? (5) Does a mid-session request include
trades before the client began requesting? (6) Is the OHLC computed server-side, independent
of the client's WebSocket subscription/connection? (7) Does a WS disconnect/reconnect have
no effect on REST OHLC? (8) Do the values reset per trading day? (9) Are the values traded-
market statistics rather than order-book/indicative? (10) Is `ohlc.open` identical in
semantics to the official daily-candle open for the same date?

## Controlled observation plan (design only — requires explicit authorization; not run)

Run only through the existing credential-gated harness with explicit user authorization;
no secrets printed. **Oracle:** official NSE opening price / an independent authoritative
current quote. **Sessions:** ≥3 distinct trading days (one with a clear morning trend so
high/low actually move).
- **A (open):** after 09:15, compare REST `ohlc.open` to the NSE official open. PASS = exact match (canonical precision) every session; FAIL = any mismatch; INCONCLUSIVE = oracle unavailable.
- **E (mid-session):** at ~11:30 on a fresh process, first REST OHLC vs the independent current quote. PASS = high/low already reflect the pre-11:30 extremes.
- **C/D (evolution):** REST snapshots through the session; high non-decreasing, low non-increasing. Needs genuine price movement to be conclusive (else INCONCLUSIVE).
- **F/G (independence):** disconnect the local WS for a window with market movement, then REST; PASS = OHLC reflects the gap's trades. INCONCLUSIVE if no movement.
- **H (reset):** Day N final vs Day N+1 first snapshot; PASS = no carry-over.
- **B (pre-open):** pre-open snapshot (if the endpoint responds) vs the first LIVE_SESSION snapshot; PASS = the post-open open equals the official open, not an indicative value.

Observational PASS is **L3** and, per ADR-009 D6/§20, does not become a permanent contract
on its own without governance accepting observational verification; L1/L2 remains preferred
for A/B/C/D/H.

---

## E6B re-check — 2026-08-12 (evidence closure attempt)

Additive record; the E6 verdict above is unchanged.

**Documentary re-check.** The official DhanHQ v2 Market Quote doc
(`https://dhanhq.co/docs/v2/market-quote/`) was re-fetched. The OHLC field descriptions are
unchanged and remain bare labels: open = "Market opening price of the day", high = "Day High
price", low = "Day Low price", close = "Market closing price of the day". None of the
safety-critical semantics (official-NSE-open equivalence, indicative-pre-open exclusion,
cumulative day-high, cumulative day-low, mid-session full-day coverage, per-day reset) are
stated. No attributable DhanHQ engineering/support (L2) confirmation was located.

**Live verification.** `LIVE_VERIFICATION_NOT_PERFORMED` — no credentialed probe authorized;
no real-market request issued.

**Verdict (unchanged).** ADR-008 D4: NOT SATISFIED. ADR-009 E6: GATE FAILED. Mandatory
properties A (official-open equivalence), B (pre-open exclusion), C (cumulative high), D
(cumulative low), E (mid-session coverage), F (trading-day reset) remain **NOT_PROVEN** at
the governed evidence level; only WS-independence and connection-independence are
STRUCTURALLY_PROVEN. Production `staged_observation_verified` remains **False** (fail closed).

**Per-source separation (E6A).** The E6 blocker "PER-SOURCE AUTHORITY SEPARATION REQUIRED" is
now resolved: `SessionStatisticsAuthority` carries independent `staged_observation_verified`
and `tick_aggregate_verified` bits, so a future verified REST source can be enabled without
elevating the unverified WebSocket path. Both bits remain False in production.

**Additional runtime finding (independent of evidence).** The production composition root
(`app/main.py`) wires no provider adapter, no `TickEngine`, and no session-statistics refresh
driver; `SessionStatisticsRefreshCoordinator.refresh_if_due` has no production caller.
Therefore, even had the evidence gate passed, the feature could not be operational:
`PRODUCTION_REFRESH_DRIVER_MISSING`. This is a separate, governed follow-up slice
(composition wiring of provider → refresh service → coordinator → scheduler), not part of
E6B.

---

## E6B-R1 re-check — 2026-08-13 (evidence closure recheck)

Additive record; the E6/E6B verdicts above are unchanged.

**Runtime status (context).** The composition wiring gap noted above is now closed: the
production runtime pipeline (provider → universe → registry → TickEngine → managed ingestion
→ Strategy Manager → RequirementsCoordinator → `SessionStatisticsRefreshCoordinator` →
managed `SessionStatisticsRefreshDriver`) is complete and lifecycle-owned (RUN-A…RUN-E,
P4.6E7). `PRODUCTION_REFRESH_DRIVER_MISSING` no longer applies. This does not change any
provider-evidence verdict.

**Documentary re-check.** The official DhanHQ v2 Market Quote doc
(`https://dhanhq.co/docs/v2/market-quote/`) was re-fetched on 2026-08-13. The OHLC field
descriptions are unchanged and remain bare labels: open = "Market opening price of the day",
high = "Day High price", low = "Day Low price", close = "Market closing price of the day".
None of the six mandatory semantics (A official-open equivalence, B indicative-pre-open
exclusion, C cumulative day-high, D cumulative day-low, E mid-session full-day coverage,
F trading-day reset) are stated at L1.

**L2.** `L2_EVIDENCE_NOT_AVAILABLE` — no attributable DhanHQ engineering/support response has
been recorded since E6B; the §Support-questions remain unanswered.

**L3.** `LIVE_VERIFICATION_NOT_AUTHORIZED` — no explicit live-verification authorization is
present in this task; no credentialed probe was run.

**Security.** No `DHAN_*` secret read, no TOTP generated, no authenticated/real-market
request issued, no secret printed. The only network access was a public GET of the docs page.

**Verdict (unchanged).** A/B/C/D/E/F all **NOT_PROVEN**. ADR-008 D4: NOT SATISFIED.
ADR-009 gate: FAILED. `PROVIDER_EVIDENCE_GATE = FAILED`. Production authority remains
`SessionStatisticsAuthority(staged_observation_verified=False, tick_aggregate_verified=False)`
(fail closed). No production code or authority default changed. P4.6E6C stays BLOCKED on
provider evidence; enablement will be a separate reviewable slice once every mandatory
property reaches its governed level.

### Outstanding L2 provider questionnaire — status PENDING

A refined, NSE_EQ-scoped clarification request to DhanHQ Support/Engineering has been
prepared to obtain attributable L2 confirmation of the mandatory properties. It asks for
independent YES/NO answers with attribution (responder name/role, date) and an explicit
"cannot be guaranteed" fallback rather than typical-behaviour answers. Question → property
mapping:

| Q | Question (NSE_EQ, `POST /v2/marketfeed/ohlc`) | Property |
|---|---|---|
| 1 | `ohlc.open` == official NSE regular-session opening price (pre-open equilibrium) | A |
| 2 | post-open `ohlc.open` excludes indicative/intermediate pre-open values | B |
| 3 | `ohlc.high` = cumulative session-start→request max (not from client connect/subscribe/query) | C |
| 4 | `ohlc.low` = cumulative session-start→request min (not from client connect/subscribe/query) | D |
| 5 | first mid-session (11:30 IST) query returns whole-day O/H/L (server-side, not client-start) | E |
| 6 | O/H/L reset per NSE trading day; no previous-day carry-forward | F |
| 7 | REST OHLC maintained independently of our WebSocket connection history (survives a 30-min disconnect) | connection independence |
| 8 | WebSocket QUOTE/FULL Day-OHLC has identical trading-day semantics to REST OHLC | REST↔WS parity (informs the separately-gated tick source; does not enable it) |

**Status:** awaiting an attributable DhanHQ written response. **Qualification rule:** a
property is elevated to L2 only by an explicit, attributable YES for its question; hedged or
undated answers do not qualify, and the gate requires all of A–F (no averaging). On receipt,
the verbatim response (source, role, date, exact question, exact answer) will be recorded
here and the P4.6E6B-R1 gate re-run. A YES to Q8 does **not** enable the tick/WebSocket source
— `tick_aggregate_verified` remains independently gated (E6A / P4.6D).

## Evidence-closure re-check — 2026-08-19 (CURRENT-SESSION-OHLC-AUTHORITY-EVIDENCE-CLOSURE-R1)

Re-inspected all repository evidence for the REST/staged source. **No change since E6B-R1.**

- `NEW_L1_EVIDENCE = NONE` — no new official DhanHQ documentation recorded; no evidence artifact dated after 2026-08-13.
- `DHAN_L2_RESPONSE = NOT_AVAILABLE` — the §Support questionnaire (Q1–Q8) remains **UNANSWERED**; no attributable DhanHQ written response exists in the repository.
- `L3_AUTHORIZATION = NOT_GRANTED` (this task carries no explicit live-verification authorization); `L3_EXECUTION = NOT_RUN`; no credential read, no authenticated request.
- `INTRADAY_HIGH_LOW_ORACLE = UNRESOLVED` (official OPEN oracle = NSE pre-open equilibrium, RESOLVED). L3 alone cannot close C/D/E without a governed independent intraday reference; Dhan-REST-vs-Dhan-WS parity is not correctness proof.
- Mandatory REST properties **A, B, C, D, F remain NOT_PROVEN** (E CONDITIONALLY_PROVEN). `REST_AUTHORITY_GATE = FAILED / BLOCKED` (no majority averaging).
- Production authority unchanged and verified in code: `SessionStatisticsAuthority()` → `staged_observation_verified=False`; `supports_current_day=False`. No merged provider-wide flag exists. Fail closed.
- No production code changed. Next closure requires an attributable DhanHQ L2 answer **or** a resolved intraday oracle plus authorized L3 observation.
