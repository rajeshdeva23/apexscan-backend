# Phase-H single-owner migration roadmap (amendment)

**Status:** design / roadmap amendment only — no live Dhan, no production, no activation. Governed by
**ADR-027** (Single-Dhan-Owner Migration Policy), which amends ADR-025/026. This document is the
operational companion to ADR-027: it holds the cutover plan, gap analysis, abort conditions, and
prerequisites. The *decision* lives in ADR-027.

## Why the original H3E plan is blocked

ADR-026 deferred the live shadow window (`B6 = NO_LIVE_H3_YET`) expecting it to clear via a second
Dhan account (Option A) or documented same-`client_id` concurrency (Option B). **H3D established:**

- `SAME_CLIENT_CONCURRENT_FEED_SUPPORT = UNKNOWN` — official DhanHQ docs do not confirm that two
  same-`client_id` authenticated feed sessions coexist, nor whether a new token invalidates the prior
  one (token is process-memory-only; ~2-min generation cooldown).
- Enabling the ingestion service **at all** makes it a live Dhan owner (token + WS) — so a live
  side-by-side shadow = **two live Dhan owners on one identity**, whose safety is unverified.
- H3D **recommended** Option A (a separate shadow identity) as the cleanest route, but it is **not
  provisioned or approved today**.

**This amendment decides** (not an H3D finding) not to make a second Dhan identity a prerequisite:
we do **not** build the roadmap on simultaneous ownership. If a second identity is later provisioned,
H3D's Option A remains the recommended path and this policy can be revisited.

## The single-owner rule

**`SINGLE_DHAN_OWNER = TRUE`** — at any time, backend **XOR** ingestion owns the live Dhan
connection, never both (ADR-027). The destination architecture is unchanged; only the *migration
strategy* changes: the IPC path is validated **offline** (replay/synthetic/recorded), and the first
live ingestion Dhan activation **is** the governed single-owner cutover.

## Revised roadmap (see ADR-027 for the full phase map)

`H3E = SUPERSEDED` → **FIX-2 track** → `H4A` consumer composition (isolated Redis) → `H4B` failure/
recovery → `H4C` shadow-compare over replay/fixtures → `H4D` consumer readiness review → `H5`
offline/replay parity + cutover evidence prep → `H6` backend-restart behavior (test topology) → `H7`
ingestion-restart/new-epoch (test topology) → `H8A` B2 / `H8B` B4 / `H8C` B11 / `H8D` authority
readiness → `H9A` cutover prep (single-owner interlock) → `H9B` governed ownership transfer → `H9C`
authority verification + rollback gate → `H10` remove backend Dhan ownership/secrets.

### FIX-2 placement

FIX-2 is a **separate track**. H4A/H4B (consumer composition, pending recovery, failure hardening)
can proceed **before** FIX-2. Any gate that asserts **timestamp correctness** or **consume-compare
parity** against legacy (H4C parity claims, H5, H9C authority verification) **requires FIX-2 resolved
and validated** first. FIX-2 is not implemented here. (This *refines* H3D §23's coarser "FIX-2
required before any consume-side comparison (H4)": the non-comparison H4A/H4B sub-phases are
unblocked; only the comparison/timestamp gates depend on FIX-2.)

### How much of H4 needs no real Dhan

All of H4A–D can be built and proven with **isolated/test Redis + replay/synthetic/recorded
fixtures**: consumer composition + C1 wiring (H4A), pending-recovery / XAUTOCLAIM / failure hardening
(H4B), and the shadow-compare framework (H4C) driven by recorded canonical events — no live Dhan, no
producer running live. Live activation of the consumer/authority is **not** part of H4.

## Future cutover topology (frozen conceptually — DO NOT EXECUTE)

A one-time, governed, verified ownership transfer. Only one Dhan owner exists at any step:

1. Backend remains authoritative **and** the sole Dhan owner.
2. Consumer/backend IPC path fully bootstrapped but **non-authoritative**, proven with replay/test
   evidence (H4–H5); IPC consumer still off in production.
3. Maintenance/cutover window begins (governed, announced).
4. If required, pause new strategy/trading decisions.
5. Stop the backend Dhan provider.
6. **Verify** the backend Dhan connection is fully closed (auth session + WS torn down) — a hard
   gate; do not proceed otherwise.
7. Start `market-ingestion` as the **sole** Dhan owner (generates its own fresh token, opens WS).
8. Verify Redis producer health (M1 epoch allocated, M2 accepting, D1 publishing, L1 healthy).
9. Backend consumer catches up / validates continuity from Redis (C1 dedup, reference bootstrap).
10. Switch the IPC path authoritative (backend consumes IPC as the market source).
11. Validate TickEngine/MarketContext health + parity.
12. Keep the legacy Dhan path disabled.

The exact live procedure, timings, and operator runbook are designed later (H9A). This document
freezes the *shape* and its gates, not an executable procedure.

## Ownership-transfer feed gap (do not hand-wave)

Steps 5→7 create a **finite live-feed gap** (backend Dhan stopped before ingestion connects). The
cutover design (H9A) must:

- **Measure** it — wall-clock from backend-WS-close to first ingestion canonical event.
- **Detect** it — L1 continuity + producer-sequence start + first-event timestamp vs the last
  legacy event.
- **Recover/catch up** where broker historical APIs permit (the market-history service can backfill
  bars for the gap window; intraday tick gap may be unrecoverable and must be surfaced, not hidden).
- **Decide acceptability** — a governed judgment (e.g. run the cutover outside the live session, or
  accept a bounded gap with a recorded reason). The gap is the deliberate cost of never having two
  owners; it is bounded and measured, never silently ignored.

## Token lifecycle at cutover & rollback

- **Token at cutover:** ingestion generates its own fresh token (process-memory-only; cross-process
  token reuse is not in the current design — an operational dependency to confirm, not an
  assumption). Subject to the ~2-min generation cooldown.
- **Rollback:** stop ingestion → ensure the Dhan owner marker is cleared → restore backend Dhan
  ownership → verify feed. Restoring the backend **regenerates a token** (cooldown-bounded gap). A
  rollback requiring another token generation is **operationally risky until proven** and must be
  rehearsed at H9A before any live H9B.

## Cutover prerequisites (all required before H9B)

- FIX-2 resolved + validated (for any timestamp/parity gate).
- Ingestion producer stack proven (H3A–C).
- Consumer/C1 stack proven (H4A–D).
- Pending recovery / redelivery proven (H4B).
- Authoritative-sink duplicate-safety resolved (B2 / H8A).
- Retention vs dedup horizon resolved (B4 / H8B).
- Redis durability + consume-side loss detection resolved (B11 / H8C).
- Consumer catch-up/backlog behavior proven (H5).
- Reference bootstrap proven (D1 loader on cutover).
- Rollback procedure frozen **and rehearsed** (H9A).
- **Single-owner interlock implemented** (ADR-027 open decision I1/I2) so two owners are impossible.
- Backend-restart-no-Dhan behavior proven (H6); ingestion-restart/new-epoch proven (H7).

## Cutover abort conditions (any → abort, restore backend)

Ingestion cannot authenticate · cannot connect WS · universe incomplete · framing errors · timestamp
correctness failure · M2 overflow · D1 publication failure · Redis unavailable · L1 terminal · the
consumer cannot catch up · a duplicate/loss invariant violation · reference bootstrap failure ·
TickEngine parity failure.

## Remaining blockers (see ADR-027 for the table)

- **B6** — `RESOLVED_BY_MIGRATION_POLICY` (design): concurrency no longer on the critical path.
  Runtime `NEVER_TWO_LIVE_DHAN_OWNERS` guarantee pends the single-owner interlock (open decision).
- **B2 / B4 / B11** — mandatory before IPC authority (H8A/B/C).
- **B10** — staged secret migration; completes at H10 after a proven cutover.

## Not in this amendment

No implementation, no real Dhan, no production, no consumer/authority activation, no H4 start, no
FIX-2, no interlock code, no destination-architecture change. `READY_FOR_LIVE_CUTOVER = NO`,
`READY_FOR_IPC_AUTHORITY = NO`.
