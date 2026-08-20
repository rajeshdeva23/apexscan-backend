# ADR-009 Addendum — Current-Session OHLC Authority Enablement Path (CSOA)

| Field | Value |
|-------|-------|
| **Type** | Subordinate governance addendum (not a numbered ADR) |
| **Subordinate to** | ADR-009 (REST-backed authoritative session statistics) and ADR-008 (authoritative current-session statistics) |
| **Related** | ADR-008/009 provider-verification records, ADR-008 evidence-closure plan, ADR-010 (runtime composition), ADR-011 (calendar authority), ADR-012 (scanner), ADR-013 (registration), NEXT-STRATEGY-SELECTION-GOV-R1 |
| **Status** | Accepted (path governance only) — **no authority bit changed; both sources remain False** |
| **Date** | 2026-08-19 |
| **Deciders** | Market-Engine / Platform Architecture |
| **Decision** | Govern the exact evidence-backed path to (eventually) enable **one independently verified** current-session OHLC source, unblocking Open=High / Open=Low. This addendum changes no code and flips no bit; it consolidates the closure conditions. |

---

## Context (audited state, 2026-08-19)

Both canonical sources remain fail-closed in code and unverified in evidence:

- Runtime wires `SessionStatisticsAuthority()` → `staged_observation_verified=False`, `tick_aggregate_verified=False` (`services/market_runtime.py:314`; injected into the tick engine at `:322` and consumed at `:538-539`; tick-engine default `_DISABLED_SESSION_STATISTICS_AUTHORITY` at `tick_engine.py:47`).
- `apply_session_ohlc` retains prior / returns `None` whenever `source_verified` is False (`session_statistics.py`), so no `AUTHORITATIVE` snapshot can form.
- Strategy readiness fails closed: a `SESSION_STATISTICS` consumer whose snapshot is not `AUTHORITATIVE` returns `SESSION_STATISTICS_NOT_AUTHORITATIVE` (`strategy_manager/readiness.py`).
- Current-day historical reconstruction stays disabled: `supports_current_day=False`; current-day intervals classify `current_day` and are withheld, never repaired (`historical/service.py`).
- **REST/staged verdict: BLOCKED / GATE FAILED** — A/B/C/D/H `NOT_PROVEN`, E `CONDITIONALLY_PROVEN` (ADR-009 provider-verification-record; E6B-R1: A–F `NOT_PROVEN`).
- **WS/tick verdict: NOT SUFFICIENT** — 9 semantic properties `NOT_PROVEN`; only field-carriage is proven (ADR-008 provider-verification-record).
- **L2_EVIDENCE = NOT_AVAILABLE** (no attributable DhanHQ written response; questionnaire unanswered). **L3 never authorized/executed.**

## Decisions CSOA1–CSOA24

- **CSOA1 — Canonical OHLC target.** Authority means the provider's *semantics* map into the canonical `SessionStatistics` contract (regular-session official open; session-to-date high/low through `as_of`; same trading session), **not** "the provider has open/high/low fields." (Preserves ADR-008 D4 / ADR-009 D6-D7.)
- **CSOA2 — Mandatory semantic properties.** The existing governed set is authoritative and unchanged: official-session-open, pre-open exclusion, session-to-date high, session-to-date low, mid-session coverage, trading-day reset, plus reconnect/local-observation independence and OHLC structural coherence. Every mandatory property must pass **independently** — no majority voting.
- **CSOA3 — REST source boundary.** REST/staged path: provider REST OHLC → `dhan/normalizer.py::_session_ohlc_from` → `SessionStatisticsRefreshService` → staged observation → activation → `apply_session_ohlc(source_verified=staged_observation_verified)`.
- **CSOA4 — Tick source boundary.** WS/tick path: `dhan/live.py::_session_ohlc` (fail-closed on uninitialised values) → `Tick.session_ohlc` → `tick_engine.py` → `apply_session_ohlc(source_verified=tick_aggregate_verified)`.
- **CSOA5 — Source separation (E6A) preserved.** REST evidence may enable **only** `staged_observation_verified`; tick evidence **only** `tick_aggregate_verified`. A merged provider-wide flag is forbidden. Verified in code: no `provider_verified`/merged boolean exists.
- **CSOA6 — Evidence hierarchy.** Use the existing L1–L4 scale (ADR-008 evidence-closure-plan): L1 official docs guarantee; L2 attributable provider engineering/support confirmation; L3 repeated controlled observation vs independent oracle; L4 single observation/inference (insufficient). Enablement prefers L1, accepts precise L2; L3 alone requires explicit governance acceptance.
- **CSOA7 — REST evidence gate.** `REST_AUTHORITY_GATE = FAILED` (A/B/C/D/H `NOT_PROVEN`, E `CONDITIONALLY_PROVEN`). `staged_observation_verified` stays **False**.
- **CSOA8 — WS evidence gate.** `TICK_AUTHORITY_GATE = FAILED` (9 semantic properties `NOT_PROVEN`). `tick_aggregate_verified` stays **False**.
- **CSOA9 — Independent oracle.** OPEN: the NSE pre-open call-auction **equilibrium price** (independent of Dhan) — RESOLVED. Intraday session-to-date HIGH/LOW at arbitrary `t`: **UNRESOLVED** — Dhan-REST-vs-Dhan-WS is parity only; NSE end-of-day Bhavcopy validates only the *final* close extrema, not mid-session accumulation. Closing C/D/E via L3 requires either a governed independent intraday reference or L2 written confirmation.
- **CSOA10 — Controlled-observation protocol.** ≥3 distinct trading days (one with a clear morning trend so extrema move), a cross-day reset pair (Day N vs N+1), a pre-open/post-open transition test, and a mid-session first-observation (fresh query/subscription ~11:30 IST). REST and WS evaluated independently. Read-only market data; no orders.
- **CSOA11 — Official-open verification.** `provider_open == NSE equilibrium open` to canonical precision (exact Decimal), for selected NSE cash-equity instruments after regular open; handle delayed-first-trade / no-trade symbols; exclude special sessions.
- **CSOA12 — Pre-open transition.** Prove the post-open `open` is the regular-session official open, not a residual indicative pre-open value; reconcile explicitly with NSE auction-equilibrium semantics rather than assuming a difference.
- **CSOA13 — Session high/low accumulation.** Prove `provider_high(t)`/`provider_low(t)` equal the session-start-through-`t` extrema; observe at multiple times; oracle must cover the same window (see CSOA9 UNRESOLVED caveat).
- **CSOA14 — Mid-session coverage.** First observation started mid-session (fresh REST query / fresh WS subscription) must already include history from market open — proving server-maintained aggregation, not client-local accumulation.
- **CSOA15 — Trading-day reset.** Across consecutive sessions (incl. a weekend/holiday boundary where feasible) open/high/low must not carry previous-day values; use authoritative `trading_date`/session identity, never `date.today()`.
- **CSOA16 — Reconnect semantics (WS).** After a governed disconnect/reconnect during `LIVE_SESSION`, the first valid post-reconnect aggregate must remain session-to-date (no reset to reconnect time); the application grants no synthetic continuity. Unproven reconnect ⇒ WS authority stays blocked.
- **CSOA17 — Special-session scope.** Authority, when eventually granted, is scoped to **ordinary NSE cash-equity sessions**; Muhurat / special Saturday / budget / altered-timing sessions are excluded unless separately evidenced. Confirm compatibility with ADR-011; if the authority primitive cannot represent this scope, that is a governance issue to raise, not a reason to broaden authority.
- **CSOA18 — Freshness vs authority.** Kept distinct: a fresh observation from an unverified source is still non-authoritative; an authoritative snapshot older than the declared `max_age` still fails readiness. (`readiness.py` enforces authority then freshness.)
- **CSOA19 — Readiness isolation.** Provider semantic authority is a **strategy/fact-level** gate, not application health: the runtime stays healthy while Open=High/Low is unavailable. Do not escalate authority failure to global app-health.
- **CSOA20 — Enablement mechanism.** Prefer an **evidence-backed composition capability**: the runtime may construct `SessionStatisticsAuthority(<bit>=True)` for a source **only** when an Accepted attributable evidence artifact records that source's PASS. Reject a plain operator flag (e.g. `SESSION_STATS_VERIFIED=true`) that could bypass evidence. Smallest change: a composition-time decision tied to the evidence artifact; no Market-Engine redesign.
- **CSOA21 — Fail-closed regression matrix.** Future tests must prove: REST-only verified ⇒ staged authoritative, tick not; WS-only verified ⇒ tick authoritative, staged not; both unverified ⇒ neither; malformed/missing/stale/wrong-date ⇒ no fabrication / fail closed; reconnect with uncertain continuity ⇒ no grant; REST/WS disagreement ⇒ existing ADR-009 D7 precedence (no invented rule); app-healthy-but-facts-unavailable ⇒ strategy not-ready only.
- **CSOA22 — Open=High/Low unblock condition.** `OPEN_EXTREME_STRATEGY_GATE = PASS` iff: (1) ≥1 source proves **all** its mandatory properties; (2) recorded in an Accepted attributable evidence artifact; (3) a separate enablement slice flips **only** that source's bit; (4) authority E2E proves the runtime yields `AUTHORITATIVE` SessionStatistics from it; (5) strategy readiness observes those facts during `LIVE_SESSION`. Evidence PASS alone does **not** unblock implementation.
- **CSOA23 — Security.** With `L3_AUTHORIZATION = NOT_GRANTED`: no credential/PIN/TOTP/access-token reads, no authenticated REST/WS, no orders, no secrets in logs/docs/tests. A future authorized L3 uses the existing credential-gated read-only harness; secrets never persisted in evidence.
- **CSOA24 — Parallel authority-independent track.** While blocked, continue value delivery with a **completed-session-only** strategy that needs no current-day authority (see report §46).

## Consequences

- Both authority bits remain **False**; Open=High/Low remains **BLOCKED**; current-day historical stays disabled.
- The closure path is explicit: pursue attributable **L2** confirmation (the cleanest, oracle-free route) and/or an **authorized L3** controlled observation; resolve the intraday high/low oracle (CSOA9) before L3 can close C/D/E.
- No source may be enabled except through CSOA20 backed by an Accepted evidence artifact.
