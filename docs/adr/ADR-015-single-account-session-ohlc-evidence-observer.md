# ADR-015 — Single-Account In-Process Session-OHLC Evidence Observer (DEPLOY-10 R4D)

| Field | Value |
|-------|-------|
| **Status** | Proposed (evidence-collection architecture; flips no authority bit, enables no strategy) |
| **Date** | 2026-08-31 |
| **Deciders** | Provider Evidence / Platform Architecture |
| **Complements** | ADR-008 (authoritative provider session statistics; tick-aggregate evidence procedure), ADR-009 (REST-backed authority; CSOA enable path), ADR-014 (float32 canonicalization & resumable collection) |
| **Related** | CSOA9, CSOA16, CSOA20, CSOA22; DEPLOY-10 R4A–R4C |

---

## Context

R4C merged a schema-2.1.0 evidence tool whose live collector opens its **own** Dhan
MarketFeed WebSocket and authenticates independently (an out-of-process diagnostic). R4D
preflight established that the operator has **only one Dhan account**. Dhan permits one
active access token per user; generating/renewing a token for that identity expires the
production token, and opening a second WS requires that token. Therefore a separate
diagnostic identity is impossible and the out-of-process WS collector is unsafe for R4D.

Static analysis (DEPLOY-10 R4D-B1/B2, verified against source) found that production already
exposes everything needed, read-only, in-process:

- `TickEngine.process` publishes immutable `MarketContextCreated`/`MarketContextUpdated`
  events on the in-process `EventBus` (`app/market_engine/tick_engine.py:223,236`), each
  carrying a frozen `MarketContext` with `instrument`, `trading_date`, `market_state`,
  `event_timestamp`, `observed_at`, `version`, `latest_tick` (→ `session_ohlc` O/H/L), and
  `session_statistics` (`app/market_engine/context.py`).
- The existing `DhanRestAdapter` holds the single authenticated provider; its
  `load_session_statistics(...)` reuses the cached Token A via `get_access_token()` with no
  regeneration except production's own near-expiry behavior (`app/adapters/dhan/adapter.py:791`,
  `app/adapters/dhan/auth.py:145`).
- Feed continuity (natural reconnects) is already emitted as `FeedContinuityEvent`
  (CONNECTED/DISCONNECTED) through the existing sink (`dhan_runtime_composition.py`).

`EventBus.publish` is **synchronous, sequential, in-process, and has no try/except**
(`app/events/bus.py:83`): a raising or slow subscriber propagates into / blocks ingestion.

## Decision

Adopt an **in-process, read-only Session-OHLC Evidence Observer** that reuses the existing
production feed, adapter, and Token A. It collects evidence; it never decides authority.

### D1 — Single-account isolation invariants
Exactly one Dhan auth lifecycle, one production WS, one adapter/auth manager. The observer
MUST NOT: construct `DhanRestAdapter.from_settings`, construct a second `DhanAuthManager`,
generate/renew/reset a token, open another WS, disconnect the production WS, or mutate any
`Tick`/`InstrumentState`/`MarketContext`/`SessionStatistics`/authority/strategy state. The
same-identity file `/etc/apexscan/dhan-r4d.env` is unused for authentication.

### D2 — Passive bus subscription with its own isolation boundary
The observer subscribes to `MarketContextUpdated`/`MarketContextCreated` like the scanner
subscribes to `StrategyResultsPublished`. Because the bus provides **no** subscriber
isolation, the observer's handler MUST wrap all work in `try/except Exception` and never
re-raise (a failure degrades evidence only, never ingestion), and MUST be effectively O(1):
no REST, no disk I/O, no JSON, no evaluation, no blocking locks, no unbounded queue in the
callback.

### D3 — Bounded snapshot state (hot path)
The callback updates one latest immutable snapshot per instrument for the current window:
`(identity, trading_date, event_timestamp, observed_at, version, open, high, low)` extracted
from the frozen `MarketContext.latest_tick.session_ohlc`. State is O(universe), never
O(ticks). Payloads are already frozen, so retaining extracted primitive values (or the frozen
context reference) is mutation-safe.

### D4 — Window orchestration (classifier-gated internal task)
A bounded internal task (mirroring existing `market_runtime` tasks) runs EARLY/MID/LATE, each
a single ≤300 s window that finishes early on full identity coverage, with no duplicate-window
retry and explicit `pending_instruments`. Every window checks the **authoritative session
classifier + trading calendar**; wall-clock only positions cadence *within* an authoritative
`LIVE_SESSION`. A window MUST NOT execute during `MARKET_CLOSED`.

### D5 — Universe freeze
`session_expected_universe` is captured via the existing universe code path at the **first
window** of the trading_date and is immutable for that session's EARLY/MID/LATE. Coverage
compares identity **sets** (not counts).

### D6 — REST OHLC via the injected existing adapter
The runtime constructor-injects its already-built, connected `DhanRestAdapter` into the
observer. The observer calls `adapter.load_session_statistics(...)` **once per window** for
the frozen universe (the adapter's existing single-request batching), never per tick. Token A
is reused (cached `get_access_token`); the observer adds no auth/regeneration/retry policy —
any near-expiry regeneration is production's own behavior. REST timeout/partial/error →
recorded as evidence incompleteness/indeterminate; it MUST NOT propagate into the Market
Engine.

### D7 — Late-attach semantics (honest limitation)
The observer attaching mid-session is `OBSERVER_LATE_ATTACH`: its first observation per
instrument already carries session-to-date extrema (because production's long-lived
subscription accumulated them), reconciled with REST. This is **not** the same as
`PROVIDER_LATE_SUBSCRIPTION` (ADR-008 §A6), which requires a *new provider subscription*
mid-session to re-send session-to-date extrema. Observer-late-attach alone therefore does
**not** satisfy ADR-008 §A6; proving provider-late-subscription needs a controlled
mid-session resubscribe (a production action requiring separate authorization). The record
labels which kind it is; it never overclaims.

### D8 — CSOA16 via natural reconnect only
The observer records a `FeedContinuityEvent` state machine (CONNECTED→DISCONNECTED→CONNECTED)
and, for a **natural** reconnect, the pre-reconnect and first post-reconnect session O/H/L per
instrument, deterministically chosen as the last snapshot before DISCONNECTED and the first
snapshot after the following CONNECTED. Reconnect is never inferred from missing ticks alone.
The observer MUST NOT trigger a reconnect. If none occurs during the session, CSOA16 is
`INCONCLUSIVE_NO_NATURAL_RECONNECT`; a controlled reconnect requires a separate governance
decision (never automatic).

### D9 — Schema 2.2.0
Serialized evidence gains provenance/labeling fields, so the schema is bumped **2.1.0 →
2.2.0** (additive, optional, defaulted — 2.1.0 artifacts still validate; no migration; the
2026-08-31 R4B record remains immutable and REJECTED): `EvidenceRecord.production_image`;
`LateStartEvidence.kind` (`provider_late_subscription` | `observer_late_attach`) and
`observer_attach_timestamp`. Reconnect pre/post provenance already exists. No new
`VerdictOutcome` value — `INCONCLUSIVE` with a specific reason string carries no-reconnect.

### D10 — Provenance
`schema_version` and `source_sha` are code/build constants; `production_image` is read from a
deploy-injected env var (the running image tag). If unavailable at runtime it is recorded as
unknown (documented limitation) — never operator-fabricated.

### D11 — Enablement flag (evidence-only)
`session_ohlc_evidence_observer_enabled: bool = Field(default=False)` (project convention).
It gates **evidence collection only** — it is not an authority flag and never sets
`tick_aggregate_verified` / `staged_observation_verified` / `supports_current_day` /
`open_high` / `open_low`. When false: no subscription, no REST, no artifacts, zero behavioral
difference. Authority enablement remains a governed composition change (ADR-009 CSOA20).

### D12 — Artifact persistence
Never written from the callback; at window close the bounded in-memory evidence is written
via atomic temp-write + rename with restrictive permissions. The `apexscan-backend` container
currently has **no volume**, so its filesystem is ephemeral; evidence persistence **requires a
bind-mounted host volume** (e.g. host `/opt/apexscan/artifacts` → container
`artifacts/deploy10-r4d/<TRADING_DATE>/`) added at deploy time.

### D13 — Evaluator reuse
The observer only **collects** schema-2.2.0 `EvidenceRecord`s. Decision uses the existing
`canonical.py`, `models.py`, `evaluate.py` (`classify_price`, `evaluate_monotonicity`,
`evaluate_record`, `combine_records`), and `report.py` unchanged in logic. No authority
decision inside the observer.

## Consequences

- WS and REST remain Dhan-derived → `oracle_independent=False`; reusing production events does
  not create an independent oracle.
- Full ADR-008 ACCEPTED may be unreachable in a single session without (a) a natural reconnect
  (CSOA16) and (b) a separately-authorized controlled mid-session resubscribe (provider
  late-start). The observer proves the core CSOA9 comparison; late-start and reconnect may
  legitimately be INCONCLUSIVE.
- Requires a production deployment of the observer-bearing image (flag OFF first), a bind-mount
  for artifacts, and separate authorization to enable the flag.

## Rejected alternatives

1. **Second credential file with the same Dhan identity** (`dhan-r4d.env`) — same user; auth
   would contend with production's single token.
2. **A second `DhanAuthManager`** for the same identity — generates/expires Token A.
3. **A second WS using a regenerated token** — regeneration expires production's token.
4. **Exposing production Token A externally** for a shared-static-token WS — unobtainable
   read-only; expiry-coupled and fragile.
5. **Redis fanout** — no market-data pub/sub exists today; adding one is unjustified surface.
6. **A new API solely for evidence** — no existing endpoint suffices and none should be added
   just for this.
7. **Intentional production reconnect/resubscribe without separate authorization** — a
   production disruption; requires its own governance decision.

Accepted ADR-008/009 and Proposed ADR-014 are referenced, not modified.
