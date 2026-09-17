# H9C-P3 — Dhan boundary + token-handoff hardening (Gate G/H)

**Status:** OFFLINE implementation complete. Closes the post-H9B audit's Gate G (real-Dhan binding
safety), Gate H (token/session handoff safety), and the carried P1 LOW-1 (ownership-loss health
state). No real Dhan, no token generation, no deployment, no ownership/authority activation — every
mechanism is inert until a governed cutover. This document records the invariants, not live commands
(the executable H9C runbook is Gate L, still to come).

## Base
- Branch `feature/phase-h9c-p3-dhan-boundary-token-handoff` off `feature/decoupling-hardening`
  `af51391`. `main` `a6b8c68` and FIX-2A #63 untouched.

## Dhan session boundary — full inventory
Every production path that can mint a token / open REST / open or reopen a WebSocket, and its
classification after P3:

| Path | Classification | Ownership before the session op? |
|---|---|---|
| Legacy backend (`dhan_runtime_composition.compose_market_runtime`) | GOVERNED | acquire → validate → **reserve mint** → `coordinator.start` (first mint) |
| Decoupled ingestion (`market_ingestion/service.start`) | GOVERNED | acquire → validate → belt-and-braces validate → **reserve mint** → connect |
| Adapter-internal WS reconnect (`adapter._connect_live_socket`) | GOVERNED (now) | injected `live_connect_authorization` asked before **every** socket open |
| Evidence CLI (`tools/session_ohlc_evidence/collect.py`) | DIAGNOSTIC (now governed) | acquires the shared lease as `OwnerRole.DIAGNOSTIC`; refuses if a governed owner holds it |
| Offline harness / consumer runtime | TEST-ONLY / non-Dhan | n/a |

The first authenticated mint per incarnation is `get_health` → `/profile`; the instrument master is
a public unauthenticated CSV, so composition never mints. The token is process-memory only
(`SecretStr`, `repr=False`), never persisted; the httpx redactor scrubs the credential URL.

## Invariants

### Ownership before token / connect / reconnect (Gate G)
```
NO VALID OWNERSHIP → NO TOKEN MINT / NO CONNECT / NO WS RECONNECT
```
- Both governed paths: `acquire_or_fail` (acquire + validate) precedes the first token mint.
- The Dhan adapter asks its injected `live_connect_authorization` (bound to `guard.validate`) at the
  top of `_connect_live_socket` — the single choke point for the initial connect AND every internal
  reconnect — **before** reading a token or opening a socket. A refusal raises
  `ProviderNotAuthorizedError`, which the reconnect loop treats as terminal (never retried), so a
  process that lost ownership can never reopen its WebSocket. The adapter knows only "may I
  connect?"; it holds no Redis or ownership dependency (arch-test enforced).

### Token-mint cooldown persistence (Gate H)
- A separate `RedisTokenMintGuard` (key `md:provider:token:mint`) records the last mint's Redis
  **server-clock** timestamp + the reserving owner's role/instance/fence — **metadata only, never
  the token**. `reserve_mint` atomically denies (fail-closed) any mint inside
  `market_token_mint_cooldown_seconds` (default 120s, mirroring Dhan's ~2-minute limit).
- It is a SEPARATE concern from the ownership lease: the **lease** decides *who* may use Dhan; the
  **throttle** decides *when* an authorized owner may mint. It never replaces or gates ownership.
- The record self-expires at the cooldown and survives a process crash/restart, so a redeploy or a
  handoff inside the window is denied cross-process rather than crash-looping the provider.

### Handoff ordering (Gate H / ADR-030)
```
predecessor stop/disconnect → lease released → successor acquires HIGHER fence
→ reserve mint (denied inside cooldown) → connect
```
A successor reaches token mint/connect only after acquiring the fenced lease; if the predecessor
minted inside the cooldown the successor is denied (`TokenMintThrottledError`) and must wait out the
remaining window — the cooldown is never bypassed. Proven offline: single owner at all times, fence
strictly increases, no mint while unauthorized.

### Diagnostic-tool safety (Part E)
The evidence CLI competes for the SAME lease as `OwnerRole.DIAGNOSTIC`, so it is mutually exclusive
with a live backend/ingestion owner (it refuses when the lease is held) and never opens a second
concurrent Dhan session. When ownership is disabled (default) it runs unguarded exactly as before —
no interlock is active anywhere in that state.

### Redis-failure behavior
Both the ownership coordinator and the token guard fail **closed**: any Redis error or malformed
durable state means ownership/permission cannot be proven, so there is no acquire, no mint, and no
connect. An indeterminate state is never read as permission.

### Ownership-loss health (P1 LOW-1)
On an ownership-loss fail-close the ingestion service publishes a **fenced** final md:health record
(`publish_if_current`, matched on `producer_id + producer_epoch`) with `ingestion`/`transport` DOWN,
so a dead incarnation stops advertising fresh HEALTHY. It does **not** claim a terminal publication
break unless continuity actually says so, and it never clobbers a successor's fresher record.

## Unknown external behavior (LIVE-gated, not decided here)
It remains unproven whether a second same-`client_id` token mint invalidates an existing Dhan
session. P3 is designed so **safety does not depend on knowing this**: a successor reaches mint/connect
only after the predecessor's ownership/session handoff is proven (lease released/expired), and the
cooldown throttle further spaces mints. Confirming Dhan's invalidation behavior is a live-validation
item for H9C.

## ADR-030 residual risk (unchanged)
Dhan is a non-fenceable external resource: under a Redis partition a crashed/stale owner's live
socket can persist until its lease TTL expires while a successor can only acquire post-expiry — an
inherent, TTL-bounded overlap window. P3 tightens the wiring (ownership-before-every-connect,
first-failure loss detection, socket-timeout-bounded renew) but does not eliminate this documented
residual; it stays a live-cutover consideration.

## Safety
Real Dhan REST/WS? NO · Real token generated? NO · AWS/Lightsail? NO · Deployed? NO · Production env
changed? NO · Ownership/IPC authority enabled? NO · FIX-2A touched? NO. Default flag shapes build no
ownership/token Redis client and perform no Dhan activity.
