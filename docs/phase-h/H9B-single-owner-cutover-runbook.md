# H9B — single-owner Dhan cutover runbook

> # ⚠️ DRAFT — NOT AUTHORIZED FOR EXECUTION
>
> This is a **conceptual** runbook produced by H9A. **No step here has been executed.** H9B is a
> separate, explicit, live-production authorization. Executing any step requires: acceptance of this
> runbook, deployment of the ADR-030 interlock in both services, resolution of the live gates below,
> and explicit human approval. **Do not run any command from this document.** It contains no secrets
> and no literal commands by design.

## Preconditions (all must hold before H9B is authorized)

- Offline H9A PASS: cross-process interlock (ADR-030 I2) proven; cutover/rollback rehearsed;
  `MAX_CONCURRENT_PROVIDER_OWNERS == 1`.
- ADR-027 **Accepted**; ADR-030 **Accepted**; ADR-028 & ADR-029 **Accepted** (authority path).
- Interlock **deployed** in both `apexscan-market-ingestion` and `apexscan-backend`.
- Redis durability policy pinned (`appendfsync`/RDB in a `redis.conf`; B11 op-side); B11 consume-side
  loss detector wired (`md:health` producer-evidence conveyance).
- Reference bootstrap proven (D1 loader on cutover).
- FIX-2 live-correctness gate resolved (RC3 confirmed / FIX-2 as needed) — no live timestamp/parity
  claim while RC3 is INCONCLUSIVE.
- Rollback artifacts staged; a maintenance window **outside** the live session (first live Dhan
  contact ⇒ accept a bounded feed gap).

## Pre-cutover checklist (verify — do not mutate)

1. Verify the exact production SHA/digest and that `main` is the intended release.
2. Verify flags: backend `legacy_market_path_enabled=true`, all `ipc_*` false, ingestion off.
3. Verify the FIX-track live-correctness prerequisite is satisfied.
4. Verify Redis health + durability config; verify consumer readiness.
5. Verify the ownership key state = `BACKEND` owner (or `NONE`), never `INGESTION`.
6. Verify rollback artifacts + the Dhan credential/token **plan** — **do not generate a token yet**.

## Transfer (one-way, ordered; abort on any stop condition)

1. Stop the legacy (backend) Dhan intake.
2. **Prove** the old provider is disconnected/stopped (not merely "asked to stop").
3. Release / let expire the backend ownership lease.
4. **Prove** ownership state is `NONE`.
5. Ingestion acquires a **fenced** ownership lease (fails closed if it cannot).
6. **Only then** ingestion starts its Dhan provider (ownership precedes provider; it generates its
   own fresh token).

## Verify

- Exactly one owner (`INGESTION`); exactly one live Dhan session.
- Ingestion publication healthy (L1); Redis stream advancing; backend consumer healthy and catching
  up; B11 loss detector reports no reset/rewind/unaccounted publication.

## Rollback (governed; safety over availability)

1. Stop the ingestion Dhan provider.
2. **Prove** it stopped.
3. Release ingestion ownership; **prove** `NONE`.
4. Backend reacquires a fenced lease.
5. **Only then** backend starts its provider (regenerates its token — subject to the Dhan token
   cooldown hazard; the resulting feed gap must be bounded and backfilled where possible).
- Never force `BACKEND + INGESTION` to restore availability.

## Stop conditions (abort the cutover/rollback immediately)

- ownership cannot be proven; both services appear provider-active; Redis ownership state
  unavailable; fencing mismatch; a provider does not stop cleanly; ingestion cannot acquire;
  ingestion provider cannot start; the stream does not advance; the consumer is unhealthy; the FIX
  live-correctness gate is unresolved; token/auth uncertainty makes rollback unsafe.

On any stop condition, halt at the current **single-owner-or-none** state and escalate — never
proceed into a two-owner state.
