# H9C — operator rollback runbook (Gate E)

> ⚠️ **PROCEDURE ONLY — NOT AUTHORIZED FOR EXECUTION.** No step here has been run. Live rollback is
> a governed action gated on the remaining LIVE gates (see the release checklist). This document
> contains no secrets and no live credentials by design. Every command is a repository-supported
> operation; anything the repository does not yet provide is marked **[RELEASE PREREQUISITE]**.

Rollback restores the **legacy backend** as the single live Dhan owner after a decoupled activation.
Its one invariant: **at all times at most one process owns the live provider** — a 0-owner gap is
acceptable, a 2-owner overlap is never. Prefer availability loss over dual ownership.

## Signals & referents (repository-supported)

| Concern | Where |
|---|---|
| Flags (in `/etc/apexscan/*.env`, applied by container recreate) | `MARKET_PROVIDER_ENABLED`, `MARKET_INGESTION_SERVICE_ENABLED`, `IPC_PUBLISHER_ENABLED`, `IPC_CONSUMER_ENABLED`, `IPC_AUTHORITATIVE_ENABLED`, `LEGACY_MARKET_PATH_ENABLED`, `MARKET_OWNERSHIP_ENABLED` |
| Ownership lease / fence | Redis keys `md:provider:ownership`, `md:provider:ownership:fence` |
| Token-mint throttle | Redis key `md:provider:token:mint` (metadata only) |
| Producer health | Redis key `md:health` (TTL 30s; stale > 15s) |
| Event stream / reference | Redis keys `md:events`, `md:reference:<trading_date>` |
| Operator view (read-only) | `GET /api/v1/diagnostics/market-authority` (ownership/health/authority) + `GET /api/v1/health/ready` |
| Services | `apexscan-backend`, `apexscan-market-ingestion` (compose project `apexscan`) |
| Timings | lease TTL 30s, renewal 10s, token cooldown 120s (defaults; confirm the deployed `MARKET_OWNERSHIP_*`/`MARKET_TOKEN_MINT_COOLDOWN_SECONDS`) |

**Flags are not runtime-toggleable.** Activation/deactivation is by editing the external env file and
**recreating** the container (deployment ≠ activation). There is no runtime authority-flip API.

## Rollback — DECOUPLED → LEGACY BACKEND (one-way, ordered; abort on any stop condition)

1. **Stop new decoupled authority.** Stop the ingestion container so it starts no new work:
   `sudo -n docker compose -p apexscan -f <release>/docker-compose.production.yml --profile market-ingestion stop market-ingestion`
2. **Prove the ingestion provider disconnected.** `GET /api/v1/diagnostics/market-authority` →
   `ingestion_health.ingestion` = `down` (or `ingestion_health.stale=true` after ≤15s); the container
   is `Exited`. Do not proceed while it still reads `healthy`.
3. **Release / expire ownership.** A clean container stop releases the lease (the service releases on
   `stop()`). If it crashed, **wait up to the lease TTL (30s)** for `md:provider:ownership` to expire.
4. **Prove no active decoupled owner.** `redis-cli GET md:provider:ownership` = `(nil)` **or**
   diagnostics `ownership.has_owner=false`. This is the single-owner-or-none checkpoint.
5. **Backend acquires ownership.** Recreate the backend with the legacy owner config:
   `MARKET_PROVIDER_ENABLED=true`, `LEGACY_MARKET_PATH_ENABLED=true`, `MARKET_OWNERSHIP_ENABLED=true`,
   all `IPC_*` false, `MARKET_INGESTION_SERVICE_ENABLED=false`. Apply via
   `scripts/deploy/remote_update.sh backend` (same immutable image).
6. **Validate fence/owner.** diagnostics `ownership.owner_role=backend` and
   `ownership.fencing_generation` strictly greater than the pre-rollback value (a fresh acquire always
   mints a higher fence).
7. **Restore the legacy provider.** Backend startup connects Dhan and mints its own token. The
   cross-process throttle enforces the **120s cooldown**: if the decoupled owner minted recently, the
   backend mint fails closed until the window elapses — **wait the remaining cooldown, do not bypass
   it.** (This is the documented Dhan token-cooldown hazard.)
8. **Verify feed health.** `GET /api/v1/health/ready` = ready; backend logs show the provider
   connected; canonical events flowing.
9. **Verify consumer/application health.** `GET /api/v1/health` live; the TickEngine/MarketContext is
   receiving legacy-path data; no error-threshold trips.
10. **Confirm peak owner ≤ 1.** diagnostics shows exactly one owner (`backend`) and no ingestion
    owner; `md:provider:ownership` holds a single record. Rollback complete.

**[RELEASE PREREQUISITE]** A single orchestrated rollback command does not exist — the authoritative
deploy pipeline (`deploy/transport.py`) updates only the backend, and there is no automated
two-service rollback runner. Until it exists, execute the steps above by hand under change control.

## Success criteria
- `ownership.owner_role = backend`, fence increased, exactly one owner.
- Legacy provider connected; `/health/ready` ready; consumer/application healthy.
- No ingestion owner; `md:health` reflects the backend/legacy path (or is absent for the stopped
  ingestion incarnation).

## Reverse direction — LEGACY → DECOUPLED (documented, DO NOT PERFORM here)
The forward cutover is the H9C operator runbook (`H9C-operator-runbook.md`): stop legacy Dhan intake →
prove disconnect → release/prove-NONE → ingestion acquires a fenced lease → reserve token mint
(cooldown) → ingestion connects → verify single owner + parity + soak. It is prohibited until the
LIVE gates close.

## Rollback triggers (Part C)

**MANDATORY ROLLBACK** (halt at single-owner-or-none, then run the sequence above):
- ownership conflict / both services appear provider-active (`ownership` shows the wrong role, or two
  live sessions);
- unexpected owner or a fence that did not increase across a handoff;
- ingestion `down` / `md:health` absent or stale during an expected live session;
- provider disconnect with reconnect refused/failed (ownership lost → `ProviderNotAuthorizedError`);
- consumer cannot catch up (persistent lag / growing PEL);
- B11 `authority.state` in a loss state (`redis_stream_reset`, `redis_state_rewind`,
  `published_event_unaccounted_for`) or `ready=false` when readiness was required;
- unexpected publication continuity break (`terminal_publication_break=true`);
- token/session handoff failure (mint refused past the cooldown, or auth failure);
- FIX-2 / timestamp correctness failure (future-timestamp rejections) — **do not blind-subtract 5:30;
  a fix requires captured raw-LTT evidence (Gate A)**;
- same-revision or ownership-config parity failure between the two services
  (`deploy/two_service_preflight.py` fails).

**WARNING** (investigate; do not necessarily roll back):
- a single transient provider reconnect that recovers within the reconnect budget;
- `authority.state = consumer_lagging` that is actively closing;
- a brief 0-owner gap during a governed handoff (expected, bounded by the TTL).

Numeric thresholds are only those already in the implementation/config (lease TTL 30s, renewal 10s,
token cooldown 120s, health stale 15s / TTL 30s). Do not invent others.
