# H9C — operator cutover runbook (Gate L)

> ⚠️ **PROCEDURE ONLY — ACTUAL CUTOVER IS PROHIBITED.** This is the operator procedure for the
> legacy→decoupled single-owner cutover plus the IPC-authoritative switch. It **must not be executed**
> until every LIVE gate below is closed (see the release checklist). No secrets, no live credentials.
> Repository-supported commands are shown; anything not yet provided is **[RELEASE PREREQUISITE]**.
> Rollback is a separate document (`H9C-rollback-runbook.md`).

**Deployment ≠ activation.** Deploying the code (both services, same immutable image) changes nothing
about authority. Authority is enabled only by editing `/etc/apexscan/*.env` and recreating the
relevant container, in the ordered CUTOVER/ACTIVATION steps — never as a side effect of a deploy.

## LIVE-GATED — NOT RESOLVED (do not mark PASS; do not invent results)
- **Gate A — FIX-2 / raw-LTT evidence.** Live timestamp correctness is UNPROVEN (RC3 inconclusive).
  A live session can be rejected for future-timestamps. **DO NOT blindly subtract 5:30** — any
  timestamp conversion must be based on captured raw-LTT evidence from a live session.
- **Gate F (live) — two-service deployment / on-host preflight.** The authoritative pipeline deploys
  only the backend; the two-service live deploy + on-host `two_service_preflight` run are not yet
  automated. **[RELEASE PREREQUISITE]**
- **Gate G/H (live) — real Dhan token/session/reconnect behavior.** Whether a second same-`client_id`
  mint invalidates an existing session is UNKNOWN and unverified; real reconnect/cooldown behavior is
  unobserved. Offline safety is proven; live behavior is not.

## Stages

### 1. PRE-DEPLOY
- Confirm the release SHA and that `main` is the intended production baseline.
- Confirm all LIVE gates above are closed with recorded evidence. If any is open → **STOP**.
- Stage rollback artifacts and the rollback runbook; schedule a window OUTSIDE the live session (the
  first live Dhan contact accepts a bounded feed gap).

### 2. DEPLOY AUTHORITY-OFF
- Build one immutable digest-pinned image (`${APEXSCAN_IMAGE}`).
- Deploy the SAME image to both services, authority OFF:
  `scripts/deploy/remote_update.sh both`. All flags remain: `LEGACY_MARKET_PATH_ENABLED=true`,
  `MARKET_PROVIDER_ENABLED=true` (legacy still authoritative), every `IPC_*` false,
  `MARKET_OWNERSHIP_ENABLED=false`, `MARKET_INGESTION_SERVICE_ENABLED=false`.
- The ingestion container is profile-gated and inert (idles); starting it activates nothing.

### 3. ON-HOST PREFLIGHT
- Prove both services run the identical revision and consistent ownership config, authority off, with
  `deploy/two_service_preflight.py` (same-revision + `REDIS_URL`/`MARKET_OWNERSHIP_*` parity +
  authority-off). **[RELEASE PREREQUISITE: an on-host runner that feeds it `docker compose config`.]**
- Verify Redis health + durability (`appendonly`), and `GET /api/v1/diagnostics/market-authority`
  shows `ownership.has_owner=false` (or `backend`), `authority.ready=false` (expected pre-cutover).

### 4. LEGACY BASELINE
- Observe the existing legacy path healthy before changing anything: `GET /api/v1/health/ready`
  ready; canonical events flowing; ownership state `backend` or `none` (never `ingestion`).

### 5. CUTOVER PRECHECK
- Confirm FIX-2 / timestamp gate satisfied (Gate A) — else **STOP**.
- Confirm reference bootstrap will seed on the consumer (Gate D wiring present) and B11 is composed.
- Confirm the token cooldown window is clear (`md:provider:token:mint` diagnostics) so the successor
  can mint.

### 6. OWNERSHIP HANDOFF (legacy → decoupled)
1. Stop legacy Dhan intake (recreate backend with `MARKET_PROVIDER_ENABLED=false`).
2. **Prove** the legacy provider disconnected (not merely asked to stop).
3. Release / let the backend ownership lease expire; **prove** `md:provider:ownership` = NONE.
4. Enable + start the ingestion owner: recreate `apexscan-market-ingestion` with
   `MARKET_INGESTION_SERVICE_ENABLED=true`, `IPC_PUBLISHER_ENABLED=true`,
   `MARKET_OWNERSHIP_ENABLED=true` (+ the governed `live_h3_publish_approved` interlock).
5. Ingestion acquires a **fenced** lease (fails closed if it cannot), reserves the token mint (fails
   closed inside the cooldown), **then** connects Dhan and mints its own fresh token.

### 7. DECOUPLED ACTIVATION (IPC authority switch)
- Only after the stream is advancing and the backend consumer is caught up, flip the backend to
  consume IPC authoritatively: recreate backend with `IPC_CONSUMER_ENABLED=true`,
  `IPC_AUTHORITATIVE_ENABLED=true`, `LEGACY_MARKET_PATH_ENABLED=false`. Dual authority
  (`authoritative ∧ legacy`) is rejected fail-fast, so this is a clean single flip.

### 8. OBSERVATION
- `GET /api/v1/diagnostics/market-authority`: exactly one owner (`ingestion`), fence increased,
  `ingestion_health` healthy + fresh, `authority.ready=true`, consumer catching up, B11 in a ready
  state, no `terminal_publication_break`. Pay special attention to the **09:15–09:30 IST** open, then
  soak beyond it watching reconnects, lag, lease renewals, dup/loss, memory/task growth.

### 9. SUCCESS
- Single decoupled owner; parity vs the legacy expectation holds (Gate A dependent); consumer healthy
  and caught up; no loss/dup; stable across the soak window. Record evidence.

### 10. ROLLBACK
- On ANY stop condition (see `H9C-rollback-runbook.md` triggers) halt at the single-owner-or-none
  state and execute the rollback runbook. Never force a two-owner state to restore availability.

## Stop conditions (abort immediately)
Ownership cannot be proven; both services appear provider-active; fencing mismatch; a provider does
not stop cleanly; ingestion cannot acquire or its provider cannot start; the stream does not advance;
the consumer is unhealthy or cannot catch up; B11 reports reset/rewind/unaccounted; the FIX-2/
timestamp gate is unresolved; token/auth uncertainty makes rollback unsafe. On any: stop and escalate.
