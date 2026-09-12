# Phase-H1 — Inert Market-Ingestion Composition + Configuration Surface

Governed by **ADR-025**. H1 adds the ability to *represent* `apexscan-market-ingestion` as a
distinct service and surfaces the Phase-H configuration model — while keeping everything **inert
under all default configuration**.

> **H1 DOES NOT START MARKET INGESTION. H1 DOES NOT CONNECT TO DHAN. H1 DOES NOT ENABLE IPC.
> H1 DOES NOT CHANGE PRODUCTION BEHAVIOUR.**

## What H1 adds

- **`app/market_ingestion/mode.py`** — the single source of truth for the ADR-025 flag matrix:
  `PhaseHFlags`, `validate_phase_h_flags` (fail-fast on illegal combinations), and
  `derive_market_path_mode` → `MarketPathMode`. Pure module (no provider/Redis/M1–L1 imports, no
  I/O).
- **`app/market_ingestion/service.py`** — `MarketIngestionService`, an inert lifecycle skeleton
  (create → validate → start → stop) with statuses `DISABLED`/`NOT_STARTED`/`STOPPED` (never
  `READY`). Constructs no provider, allocates no epoch, connects no Redis.
- **`app/market_ingestion/__main__.py`** — the dedicated entrypoint `python -m app.market_ingestion`.
  Disabled (default) → logs its inert status and exits 0 with zero Dhan/IPC/Redis activity.
  Enabled → in H1 the entrypoint refused to boot (live boot was deferred to H2). **Superseded by
  H2**, which implements the real provider lifecycle — see `H2-ingestion-service-boot.md`.
- **`Settings`** — six activation flags + a fail-fast matrix validator + accessors
  (`phase_h_flags()`, `market_path_mode()`, `market_ipc_config()`).
- **Compose** — a profile-gated `market-ingestion` service in both compose files, excluded from the
  default stack, with **no public port**.

## Configuration flags and default mode

| Flag | Default |
|---|---|
| `MARKET_INGESTION_SERVICE_ENABLED` | `false` |
| `IPC_PUBLISHER_ENABLED` | `false` |
| `IPC_CONSUMER_ENABLED` | `false` |
| `IPC_SHADOW_COMPARE_ENABLED` | `false` |
| `IPC_AUTHORITATIVE_ENABLED` | `false` |
| `LEGACY_MARKET_PATH_ENABLED` | `true` |

Defaults derive **`MarketPathMode.LEGACY_ONLY`** — the current production behaviour, unchanged.

## Illegal combinations (fail-fast at settings validation; ADR-025)

- `ipc_authoritative ∧ ¬ipc_consumer` — authority with no source.
- `ipc_authoritative ∧ legacy_market_path` — dual authority into the TickEngine.
- `ipc_shadow_compare ∧ ¬ipc_consumer` — nothing to compare.
- `ipc_publisher ∧ ¬market_ingestion_service` — publisher without its producer.
- `¬legacy_market_path ∧ ¬ipc_authoritative` — backend with no market authority.
- `ipc_consumer ∧ ¬ipc_shadow_compare ∧ ¬ipc_authoritative` — a role-less consumer would drain the
  stream and record dedup keys, poisoning dedup for a later authoritative switch.

## Mode derivation (from validated flags)

`ipc_authoritative` → `IPC_AUTHORITATIVE_BACKEND` · else `ipc_shadow_compare` →
`SHADOW_CONSUME_COMPARE` · else `ipc_publisher` → `INGESTION_SHADOW_PUBLISH` · else `LEGACY_ONLY`.
(`LEGACY_PROVIDER_REMOVED` is a post-H10 code state, not flag-derivable.)

## Compose behaviour

`market-ingestion` is gated behind the `market-ingestion` profile in `docker-compose.yml` and
`docker-compose.production.yml`, so `docker compose up` (and the deploy pipeline's `up backend`)
never start it. It shares the backend image, runs `python -m app.market_ingestion`, exposes no
port, uses `restart: "no"` (a clean inert exit must not restart-loop), depends only on Redis (no
PostgreSQL), and mounts `dhan.env` (secret-ownership seam). The backend keeps its own Dhan access
until H9/H10 (B10 remains implementation-pending).

## What H1 explicitly does NOT do

No Dhan auth/token/REST/WebSocket, no M1 epoch allocation, no Redis connection/mutation, no IPC
publish/consume, no M2/C1 activation, no L1 authority gating, no shadow/dual/cutover, no FIX-2, no
TickEngine/timestamp change, no production contact. M1/D1/M2/C1/L1 semantics are unchanged. Live
boot (publisher still OFF) is **H2**.
