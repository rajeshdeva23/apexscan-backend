# H9C — release readiness checklist (Gate J)

The single source of truth for what is DONE offline versus what remains before a live H9C cutover.

> **Deployment ≠ authority activation.** Shipping the code (both services, same immutable image, all
> authority flags OFF) is safe and changes nothing about who owns Dhan. Enabling ownership / IPC
> authority is a **separate governed cutover** (`H9C-operator-runbook.md`). Nothing in "OFFLINE
> CLOSED" authorizes a live cutover.

## OFFLINE CLOSED (implemented + tested on `feature/decoupling-hardening`, inert by default)

| Gate | What | Evidence |
|---|---|---|
| **D** | Reference bootstrap wired into the consumer runtime | H9C-P1 (`consumer_runtime` loader; `test_market_ipc_h9c_p1_*`) |
| **J** | `md:health` producer writer wired into the ingestion runtime | H9C-P1 (`service._publish_health`) |
| **F (offline)** | Two-service deploy readiness: ingestion long-running-but-inert, `remote_update.sh both`, same-revision + config parity + authority-off preflight | H9C-P2 (`two_service_preflight.py`, `test_h9c_p2_*`) |
| **I** | Ownership TTL/renewal config explicit; invariant `0<renewal<ttl`; renew socket timeout | H9C-P2 (`.env.example`, settings validator) |
| **G (offline)** | Ownership before every token mint / connect / reconnect; adapter reconnect authorization; diagnostic tool participates in the shared lease | H9C-P3 (`_connect_live_socket` authz, `collect.py` DIAGNOSTIC role) |
| **H (offline)** | Persisted cross-process token-mint throttle (metadata only, fail-closed) | H9C-P3 (`token_mint_guard.py`) |
| **E** | Operator rollback runbook (DECOUPLED→LEGACY) + triggers | `H9C-rollback-runbook.md` |
| **K** | Read-only market-authority diagnostics surface (secret-free, fail-closed) | H9C-P4 (`GET /api/v1/diagnostics/market-authority`) |
| **L (offline)** | H9C operator cutover runbook (procedure only) | `H9C-operator-runbook.md` |

Also closed offline: P1 LOW-1 (fenced non-healthy `md:health` on ownership loss, H9C-P3).

## PENDING — LIVE / GOVERNANCE (must clear before a live cutover)

| Gate | What | Blocker type |
|---|---|---|
| **A** | FIX-2 / raw-LTT timestamp evidence (RC3 inconclusive) | LIVE market-hours capture. **Do NOT blindly subtract 5:30** — conversion must follow captured raw-LTT evidence. |
| **B — ADR-028** | IPC durable dedup retention (B4) | Implementation TECHNICALLY READY (conformant on-branch, not activated); **governance acceptance PENDING** (Status: Proposed). Not self-approved. |
| **C — ADR-029** | IPC Redis loss/continuity (B11) | Implementation TECHNICALLY READY; **governance acceptance PENDING** (Status: Proposed). Not self-approved. |
| **F (live)** | Actual two-service deployment + on-host preflight run; pipeline (`deploy/transport.py`) two-service automation | RELEASE PREREQUISITE (deploy pipeline currently backend-only). |
| **G/H (live)** | Real Dhan token/session/reconnect behavior; whether a 2nd same-`client_id` mint invalidates a live session | LIVE observation. Offline safety proven; live behavior unverified. |

## Release gate
A live H9C cutover is authorized only when **every** PENDING row above is closed with recorded
evidence, ADR-028/029 are governance-Accepted, and the operator has the rollback runbook staged. Until
then: deploy authority-OFF freely; **do not enable ownership or IPC authority.**
